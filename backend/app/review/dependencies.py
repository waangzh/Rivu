"""Version-bound public dependency source, separate from target-commit evidence."""

import asyncio
import json
import re
from urllib.parse import quote

import httpx

from app.review.domain import DependencyRef, SourceRef, utcnow
from app.review.evidence import EvidenceArchive, SourceReadError, digest, source_lines
from app.review.tool_scope import _validate_repository_relative_path, is_sensitive_repository_path
from app.services.review.evidence.file_context import _get, _text, _repository


def pinned_dependency(manifest_path, text, package):
    """Only exact Go module and npm lockfile versions; no inferred range resolution."""
    if manifest_path.endswith("go.mod"):
        matches = re.findall(r"(?m)^\s*(?:require\s+)?" + re.escape(package) + r"\s+(v\d+\.\d+\.\d+[^\s]*)", text)
        if len(set(matches)) != 1 or not package.startswith("github.com/"):
            raise SourceReadError("dependency_version_not_pinned")
        if re.search(r"-.*\d{14}-[0-9a-f]{12}", matches[0]) or "+" in matches[0]:
            raise SourceReadError("dependency_version_form_unsupported")
        # Replace directives change package identity; this initial channel does not resolve them.
        if re.search(r"(?m)^\s*replace\b", text):
            raise SourceReadError("dependency_replace_unsupported")
        parts = package.split("/")
        if len(parts) > 4 or (len(parts) == 4 and not re.fullmatch(r"v\d+", parts[3])):
            raise SourceReadError("dependency_submodule_unsupported")
        repo = _repository(f"https://github.com/{'/'.join(parts[1:3])}")
        return matches[0], repo
    if manifest_path.endswith("package-lock.json"):
        data = json.loads(text)
        entry = data.get("packages", {}).get(f"node_modules/{package}")
        if not isinstance(entry, dict) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.-]+)?", entry.get("version", "")):
            raise SourceReadError("dependency_version_not_pinned")
        if entry.get("link") or not entry.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise SourceReadError("dependency_registry_unsupported")
        return entry["version"], None
    raise SourceReadError("dependency_manifest_unsupported")


def read_dependency(archive: EvidenceArchive, ref: DependencyRef):
    encoded = archive.artifacts.read_text((archive.artifacts.root / archive.snapshot.review_id /
        f"dependency-{ref.id}.txt").as_uri())
    payload = json.loads(encoded)
    loaded = DependencyRef(id=digest(encoded), **{key: value for key, value in payload.items() if key != "content"})
    manifest_ref, manifest = archive.read_source(ref.manifest_source.id)
    version, repository = pinned_dependency(ref.manifest_source.path, manifest, ref.package)
    if (loaded != ref or ref.review_id != archive.snapshot.review_id or manifest_ref != ref.manifest_source
            or digest(payload["content"]) != ref.content_hash
            or len(source_lines(payload["content"])) != ref.end_line - ref.start_line + 1
            or version != ref.version or (repository is not None and repository != ref.repository)
            or ref.binding.get("version") != version or ref.binding.get("commit_sha") != ref.commit_sha
            or ref.binding.get("repository") != ref.repository
            or is_sensitive_repository_path(ref.path)):
        raise ValueError("dependency_evidence_invalid")
    return payload["content"]


async def load_dependency(context, manifest_path, package, path, start, end):
    _validate_repository_relative_path(path)
    if is_sensitive_repository_path(path) or end < start or end - start >= 240:
        raise SourceReadError("dependency_range_or_path_rejected")
    evidence = context.evidence
    # Full bounded manifest text is archived as target evidence, not a guessed version.
    manifest_sha, text = await asyncio.to_thread(evidence._source, manifest_path, "head")
    lines = source_lines(text)
    if len(lines) > 50_000:
        raise SourceReadError("dependency_manifest_too_large")
    version, repo = pinned_dependency(manifest_path, text, package)
    payload = dict(review_id=context.provider.run.id, commit_sha=manifest_sha, side="head",
        path=manifest_path, start_line=1, end_line=len(lines), content_hash=digest(text), content=text)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    artifact = await asyncio.to_thread(evidence.artifacts.put_text, task_id=context.provider.run.id,
                                      kind="evidence", content=encoded)
    manifest_source = SourceRef(id=artifact.sha256, **{key: value for key, value in payload.items() if key != "content"})
    pin_key = digest(json.dumps([manifest_source.id, package, version]))
    current = await asyncio.to_thread(context.provider.store.get, context.provider.run.id)
    pin = current.dependency_pins.get(pin_key)
    deadline = (context.provider.run.hard_deadline if context.work.kind == "verification" else
                min(context.provider.run.review_cutoff,
                    context.work.investigation_deadline or context.provider.run.review_cutoff))
    async with asyncio.timeout(min(45, max(0.1, (deadline - utcnow()).total_seconds()))):
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                    headers={"Accept": "application/vnd.github+json"}) as client:
            if pin:
                repo, sha = pin["repository"], pin["commit_sha"]
            elif repo is None:
                url = f"https://registry.npmjs.org/{quote(package, safe='')}/{quote(version, safe='')}"
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise SourceReadError("dependency_reference_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise SourceReadError("dependency_metadata_too_large")
                metadata = json.loads(body)
                repository = metadata.get("repository", {})
                url = repository.get("url", "") if isinstance(repository, dict) else ""
                url = url.removeprefix("git+")
                repo = _repository(url)
                sha = metadata.get("gitHead", "")
                if metadata.get("name") != package or metadata.get("version") != version:
                    raise SourceReadError("dependency_version_mismatch")
                binding = {"kind": "npm_git_source", "package": package, "version": version,
                           "repository": repo, "commit_sha": sha,
                           "metadata_hash": digest(json.dumps(metadata, sort_keys=True))}
            else:
                tag = await _get(client, f"/repos/{repo}/git/ref/tags/{quote(version, safe='')}", 256 * 1024)
                obj = tag.get("object", {})
                if obj.get("type") == "tag" and re.fullmatch(r"[0-9a-f]{40}", obj.get("sha", "")):
                    obj = (await _get(client, f"/repos/{repo}/git/tags/{obj['sha']}", 256 * 1024)).get("object", {})
                if obj.get("type") != "commit":
                    raise SourceReadError("dependency_commit_unavailable")
                sha = obj.get("sha", "")
                binding = {"kind": "go_module_tag", "package": package, "version": version,
                           "repository": repo, "commit_sha": sha,
                           "metadata_hash": digest(json.dumps(tag, sort_keys=True))}
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise SourceReadError("dependency_commit_unavailable")
            pin = await asyncio.to_thread(context.provider.store.freeze_dependency_pin,
                context.provider.run.id, pin_key, pin or binding, context.provider.lease)
            repo, sha = pin["repository"], pin["commit_sha"]
            fixed_lines = source_lines(await _text(client, repo, sha, path))
            if manifest_path.endswith("go.mod"):
                module = await _text(client, repo, sha, "go.mod")
                if not re.search(r"(?m)^module\s+" + re.escape(package) + r"\s*(?://[^\n]*)?$", module):
                    raise SourceReadError("dependency_module_path_mismatch")
    if end > len(fixed_lines):
        raise SourceReadError("dependency_range_invalid", total_lines=len(fixed_lines))
    content = "".join(fixed_lines[start - 1:end])
    if len(content) > evidence.max_output_chars:
        raise SourceReadError("dependency_output_too_large")
    payload = dict(review_id=context.provider.run.id, package=package, version=version,
        repository=repo, commit_sha=sha, path=path, start_line=start, end_line=end,
        content_hash=digest(content), manifest_source=manifest_source.model_dump(), binding=pin, content=content)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    await asyncio.to_thread(evidence.artifacts.put_text, task_id=context.provider.run.id,
                            kind="dependency", content=encoded)
    ref = DependencyRef(id=digest(encoded), **{key: value for key, value in payload.items() if key != "content"})
    return ref, content
