"""Frozen, independently owned Git objects from explicitly registered local repositories."""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import exists, select, update

from app.core.config import settings
from app.models.orm import LocalReviewInputOrm, ReviewTaskOrm
from app.review.domain import LocalGitSource, Snapshot, utcnow
from app.review.evidence import EvidenceService, source_lines
from app.review.planner import identity
from app.review.store import as_utc
from app.core.paths import validate_repo_relative_path
from app.review.tool_scope import is_sensitive_repository_path
from app.review.tool_scope import is_sensitive_repository_change
from app.models.file_view import FileContextResponse
from app.services.workspaces.prepared_workspaces import PreparedWorkspaces
from app.tools.git_tool import DIFF_POLICY_VERSION, GitToolCancelled


class LocalInputError(ValueError):
    pass


@dataclass
class PreparationGuard:
    cancel: object
    deadline: float

    def is_set(self):
        return time.monotonic() >= self.deadline or (self.cancel is not None and self.cancel.is_set())


class LocalInputs:
    def __init__(self, store, artifacts, git):
        self.store, self.git = store, git
        self.artifacts = artifacts
        self.root = artifacts.root / "_local_inputs"

    def _path(self, input_id):
        if not re.fullmatch(r"[0-9a-f]{32}", input_id):
            raise LocalInputError("local_input_invalid")
        path = self.root / input_id
        if (self.root.is_symlink() or getattr(self.root, "is_junction", lambda: False)()
                or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or path.resolve().parent != self.root.resolve()):
            raise LocalInputError("local_input_invalid")
        return path

    def _check(self, cancel):
        if isinstance(cancel, PreparationGuard) and time.monotonic() >= cancel.deadline:
            raise LocalInputError("local_input_preparation_timeout")
        if cancel is not None and cancel.is_set():
            raise GitToolCancelled("Git 操作已取消")

    def _diff(self, repo, base, head, cancel):
        self._check(cancel)
        try:
            result = self.git.unified_diff(repo, base, head, cancel_event=cancel)
        except GitToolCancelled:
            self._check(cancel)
            raise
        self._check(cancel)
        return result

    def _command(self, repo, arguments, cancel=None, *, limit=32 * 1024**2,
                 allow_fetch=False, input_data=b"", output_path=None, check=True):
        self._check(cancel)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
            GIT_OPTIONAL_LOCKS="0")
        protocols = ([option for name in ("https", "http", "ssh", "file")
                      for option in ("-c", f"protocol.{name}.allow=always")] if allow_fetch else [])
        environment["GIT_NO_LAZY_FETCH"] = "0" if allow_fetch else "1"
        command = [self.git._git, "-c", "protocol.allow=never", *protocols, "-c", "core.autocrlf=false",
                   "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *arguments]
        with (tempfile.TemporaryFile() as incoming,
              (output_path.open("wb") if output_path else tempfile.TemporaryFile()) as output,
              tempfile.TemporaryFile() as errors):
            incoming.write(input_data)
            incoming.seek(0)
            process = subprocess.Popen(command, stdin=incoming, stdout=output, stderr=errors,
                                       env=environment)
            deadline = time.monotonic() + self.git._command_timeout_seconds
            try:
                while process.poll() is None:
                    self._check(cancel)
                    if time.monotonic() >= deadline or max(output.tell(), errors.tell()) > limit:
                        raise LocalInputError("local_input_command_limit")
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode and check:
                    raise LocalInputError("local_input_git_failed")
                if output_path:
                    if output.tell() > limit:
                        raise LocalInputError("local_input_size_limit")
                    return b""
                output.seek(0)
                result = output.read(limit + 1)
                if len(result) > limit:
                    raise LocalInputError("local_input_command_limit")
                return result
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def _copy_objects(self, source, destination, cancel):
        raw = Path(source)
        if raw.is_symlink() or getattr(raw, "is_junction", lambda: False)():
            raise LocalInputError("local_repository_link")
        root = raw.resolve()
        git = root / ".git"
        if not git.is_dir() or git.is_symlink() or getattr(git, "is_junction", lambda: False)():
            raise LocalInputError("local_repository_requires_normal_git_directory")
        borrowed = (git / "shallow").exists() or (git / "objects/info/alternates").exists()
        total, count, directory_count = 0, 0, 0
        candidates = [git / "HEAD", git / "packed-refs"]
        if (git / "shallow").exists():
            candidates.append(git / "shallow")
        for directory in (git / "objects", git / "refs"):
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise LocalInputError("local_repository_link")
            for parent, directories, files in os.walk(directory, followlinks=False):
                self._check(cancel)
                directory_count += len(directories)
                for name in directories:
                    child = Path(parent) / name
                    if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                        raise LocalInputError("local_repository_link")
                candidates.extend(Path(parent) / name for name in files)
                if len(candidates) + directory_count > settings.rivu_local_input_max_files:
                    raise LocalInputError("local_input_too_many_files")
        borrowed = borrowed or any(path.suffix == ".promisor" for path in candidates)
        for path in candidates:
            self._check(cancel)
            if not path.exists() and path.name == "packed-refs":
                continue
            relative = path.relative_to(git).as_posix()
            if relative.startswith("refs/") and not relative.startswith(("refs/heads/", "refs/tags/")):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(git):
                raise LocalInputError("local_repository_link")
            if borrowed and relative.startswith("objects/"):
                continue
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise LocalInputError("local_repository_non_regular_file")
            total += metadata.st_size
            count += 1
            if not relative.startswith("objects/") and metadata.st_size > 8 * 1024**2:
                raise LocalInputError("local_input_metadata_limit")
            if total > settings.rivu_local_input_max_bytes or count > settings.rivu_local_input_max_files:
                raise LocalInputError("local_input_size_limit")
            target = destination / ".git" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as reader, target.open("wb") as writer:
                if os.fstat(reader.fileno()).st_ino != metadata.st_ino:
                    raise LocalInputError("local_repository_changed")
                remaining = metadata.st_size
                while remaining:
                    self._check(cancel)
                    block = reader.read(min(remaining, 1024**2))
                    if not block:
                        raise LocalInputError("local_repository_changed")
                    writer.write(block)
                    remaining -= len(block)
                if reader.read(1):
                    raise LocalInputError("local_repository_changed")
            if relative == "packed-refs":
                lines, keep = [], False
                for line in target.read_text(encoding="ascii").splitlines():
                    if line.startswith("#"):
                        continue
                    if line.startswith("^"):
                        if keep:
                            lines.append(line)
                        continue
                    fields = line.split(" ")
                    keep = len(fields) == 2 and fields[1].startswith(("refs/heads/", "refs/tags/"))
                    if keep:
                        lines.append(line)
                if lines:
                    target.write_text("\n".join(lines) + "\n", encoding="ascii")
                else:
                    target.unlink()
        if borrowed:
            alternate = destination / ".git/objects/info/alternates"
            alternate.parent.mkdir(parents=True, exist_ok=True)
            alternate.write_text(str(git / "objects") + "\n", encoding="utf-8")
            values = self._command(root, ["config", "--local", "--no-includes", "--get-regexp",
                r"^remote\..*\.(url|promisor)$"], cancel, check=False).decode("utf-8")
            config = dict(line.split(None, 1) for line in values.splitlines() if " " in line)
            remote = next((key.removesuffix(".promisor") for key, value in config.items()
                           if key.endswith(".promisor") and value.lower() == "true"), "remote.origin")
            url = config.get(remote + ".url")
            if url:
                if ":" not in url and not Path(url).is_absolute():
                    url = str((root / url).resolve())
                self._command(destination, ["config", "remote.origin.url", url], cancel)
                if any(path.suffix == ".promisor" for path in candidates):
                    self._command(destination, ["config", "remote.origin.promisor", "true"], cancel)
        return borrowed

    def _freeze_borrowed_objects(self, repo, base, head, cancel):
        if ((repo / ".git/shallow").exists()
                and not self.git.merge_bases(repo, base, head, cancel_event=cancel)
                and self._command(repo, ["config", "--get", "remote.origin.url"], cancel, check=False)):
            self._command(repo, ["fetch", "--no-tags", "--no-recurse-submodules",
                "--deepen=256", "origin", base, head], cancel, allow_fetch=True)
        pack = repo / ".git/objects/pack/snapshot.pack"
        pack.parent.mkdir(parents=True, exist_ok=True)
        self._command(repo, ["pack-objects", "--revs", "--stdout"], cancel, allow_fetch=True,
            input_data=f"{base}\n{head}\n".encode("ascii"), output_path=pack,
            limit=settings.rivu_local_input_max_bytes)
        self._command(repo, ["index-pack", str(pack)], cancel)
        for marker in pack.parent.glob("*.promisor"):
            marker.unlink()
        (repo / ".git/objects/info/alternates").unlink()
        self._command(repo, ["config", "--remove-section", "remote.origin"], cancel, check=False)
        shutil.rmtree(repo / ".git/refs")
        (repo / ".git/refs").mkdir()
        (repo / ".git/packed-refs").unlink(missing_ok=True)
        self._command(repo, ["update-ref", "--no-deref", "HEAD", head], cancel)

    def _resolve(self, repo, ref, cancel):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ref.startswith("-") or ".." in ref:
            raise LocalInputError("local_ref_invalid")
        sha = self._command(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                            cancel).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise LocalInputError("local_ref_invalid")
        return sha

    def _materialize(self, repo, head, cancel):
        self._command(repo, ["read-tree", head], cancel)
        self._command(repo, ["update-ref", "--no-deref", "HEAD", head], cancel)
        entries = self._command(repo, ["ls-tree", "-rlz", head], cancel).split(b"\0")
        if len(entries) - 1 > settings.rivu_local_input_max_files:
            raise LocalInputError("local_input_too_many_files")
        total = 0
        for encoded in entries:
            self._check(cancel)
            if not encoded:
                continue
            metadata, raw_path = encoded.split(b"\t", 1)
            mode, kind, sha, size = metadata.split()
            path = raw_path.decode("utf-8")
            if mode not in {b"100644", b"100755"} or kind != b"blob" or is_sensitive_repository_path(path):
                continue
            validate_repo_relative_path(path)
            if int(size) > 512_000:
                continue
            total += int(size)
            if total > settings.rivu_local_input_max_bytes:
                raise LocalInputError("local_input_size_limit")
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._command(repo, ["cat-file", "blob", sha.decode("ascii")],
                                             cancel, limit=512_000))

    def freeze(self, request, cancel=None):
        cancel = PreparationGuard(cancel, time.monotonic() + self.git._command_timeout_seconds)
        source = settings.rivu_local_repositories.get(request.repository_key)
        if source is None:
            raise LocalInputError("local_repository_not_registered")
        if request.input_id:
            saved, input_id = self.load(request.input_id, repository_key=request.repository_key)
            if (request.base_ref not in {saved["base_ref"], saved["declared_base_sha"]}
                    or request.head_ref not in {saved["head_ref"], saved["head_sha"]}
                    or request.title != saved.get("title") or request.body != saved.get("body", "")):
                raise LocalInputError("local_input_identity_mismatch")
            return saved, input_id
        input_id = uuid4().hex
        directory = self._path(input_id)
        repo = directory / "repo"
        with PreparedWorkspaces(self.store, self.root).guard(cancel_event=cancel):
            try:
                repo.mkdir(parents=True)
                template = directory / "template"
                template.mkdir()
                self._command(repo, ["init", "--quiet", f"--template={template}"], cancel)
                borrowed = self._copy_objects(source, repo, cancel)
                base = self._resolve(repo, request.base_ref, cancel)
                head = self._resolve(repo, request.head_ref, cancel)
                if borrowed:
                    self._freeze_borrowed_objects(repo, base, head, cancel)
                self._command(repo, ["fsck", "--no-reflogs", "--no-dangling", base, head], cancel)
                effective, diff = self._diff(repo, base, head, cancel)
                self._materialize(repo, head, cancel)
                self._check(cancel)
                manifest = {"repository_key": request.repository_key,
                    "declared_base_sha": base, "base_sha": effective, "head_sha": head,
                    "diff_policy": DIFF_POLICY_VERSION, "diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
                    "base_ref": request.base_ref, "head_ref": request.head_ref}
                if request.title is not None or request.body:
                    manifest.update(title=request.title, body=request.body)
                manifest["input_digest"] = identity(manifest)
                now = utcnow()
                with self.store.sessions.begin() as session:
                    session.add(LocalReviewInputOrm(id=input_id, manifest=manifest,
                        created_at=now, expires_at=now + timedelta(hours=24)))
            except BaseException as exc:
                shutil.rmtree(directory, ignore_errors=True)
                if isinstance(exc, (OSError, UnicodeError)):
                    raise LocalInputError("local_input_unavailable") from None
                raise
        return manifest, input_id

    def load(self, input_id, *, repository_key=None):
        with self.store.sessions() as session:
            row = session.get(LocalReviewInputOrm, input_id)
            pinned = session.scalar(select(ReviewTaskOrm.id).where(
                ReviewTaskOrm.input_id == input_id, ReviewTaskOrm.deleted_at.is_(None)).limit(1))
            if row is None or (not pinned and as_utc(row.expires_at) <= utcnow()):
                raise LocalInputError("local_input_expired")
            manifest = dict(row.manifest)
        expected = manifest.pop("input_digest")
        if (identity(manifest) != expected or manifest["diff_policy"] != DIFF_POLICY_VERSION
                or (repository_key is not None and manifest["repository_key"] != repository_key)):
            raise LocalInputError("local_input_identity_mismatch")
        manifest["input_digest"] = expected
        return manifest, input_id

    def prepare(self, manifest, input_id, run_id, cancel=None):
        cancel = PreparationGuard(cancel, time.monotonic() + self.git._command_timeout_seconds)
        with PreparedWorkspaces(self.store, self.root).guard(cancel_event=cancel):
            saved, _ = self.load(input_id, repository_key=manifest["repository_key"])
            if saved != manifest:
                raise LocalInputError("local_input_identity_mismatch")
            target = self.git._workdir / f"local-{uuid4().hex}"
            try:
                shutil.copytree(self._path(input_id) / "repo", target, symlinks=False)
                self._check(cancel)
                self._command(target, ["fsck", "--no-reflogs", "--no-dangling",
                    saved["declared_base_sha"], saved["head_sha"]], cancel)
                effective, diff = self._diff(target, saved["declared_base_sha"], saved["head_sha"], cancel)
                if effective != saved["base_sha"] or hashlib.sha256(diff.encode()).hexdigest() != saved["diff_hash"]:
                    raise LocalInputError("local_input_identity_mismatch")
                snapshot = Snapshot(review_id=run_id, repository=f"local:{saved['repository_key']}",
                    declared_base_sha=saved["declared_base_sha"], base_sha=effective, head_sha=saved["head_sha"])
                source = LocalGitSource(repository_key=saved["repository_key"], input_id=input_id,
                    title=saved.get("title") or
                          f"{saved['repository_key']}: {saved['base_ref']} → {saved['head_ref']}"[:200],
                    body=saved.get("body", ""),
                    input_digest=saved["input_digest"])
                return target, diff, snapshot, source
            except BaseException as exc:
                shutil.rmtree(target, ignore_errors=True)
                if isinstance(exc, (OSError, UnicodeError)):
                    raise LocalInputError("local_input_unavailable") from None
                raise

    def context(self, run, file_id):
        saved, input_id = self.load(run.source.input_id)
        if (saved["input_digest"] != run.source.input_digest
                or any(saved[key] != getattr(run.snapshot, key)
                       for key in ("declared_base_sha", "base_sha", "head_sha"))):
            raise LocalInputError("local_input_identity_mismatch")
        changed = run.changed_files[file_id]
        if changed.is_binary or is_sensitive_repository_change(changed.file_path, changed.old_file_path):
            raise LocalInputError("local_source_not_readable")
        paths = frozenset({changed.file_path, changed.old_file_path or changed.file_path})
        evidence = EvidenceService(snapshot=run.snapshot, artifacts=self.artifacts,
            repo_path=self._path(input_id) / "repo", allowed_paths=paths, git=self.git)
        base = "" if changed.change_type == "added" else evidence._source(
            changed.old_file_path or changed.file_path, "base")[1]
        head = "" if changed.change_type == "deleted" else evidence._source(changed.file_path, "head")[1]
        if max(len(source_lines(base)), len(source_lines(head))) > 50_000:
            raise LocalInputError("local_source_too_many_lines")
        return FileContextResponse(file_id=file_id, file_path=changed.file_path, snapshot=run.snapshot,
            base_lines=[line.removesuffix("\n") for line in source_lines(base)],
            head_lines=[line.removesuffix("\n") for line in source_lines(head)])

    def maintain(self):
        if not self.root.exists():
            return
        with PreparedWorkspaces(self.store, self.root).guard():
            with self.store.sessions.begin() as session:
                session.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.deleted_at.is_not(None))
                    .values(input_id=None))
                rows = list(session.scalars(select(LocalReviewInputOrm).where(
                    LocalReviewInputOrm.expires_at <= utcnow(),
                    ~exists().where(ReviewTaskOrm.input_id == LocalReviewInputOrm.id))))
                for row in rows:
                    shutil.rmtree(self._path(row.id), ignore_errors=True)
                    session.delete(row)
            with self.store.sessions() as session:
                known = set(session.scalars(select(LocalReviewInputOrm.id)))
            for path in self.root.iterdir():
                if (re.fullmatch(r"[0-9a-f]{32}", path.name) and path.name not in known
                        and path.stat().st_mtime < time.time() - 86400):
                    shutil.rmtree(self._path(path.name), ignore_errors=True)
