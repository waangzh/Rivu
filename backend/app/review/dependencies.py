"""Validate dependency evidence in retained reports; no network acquisition."""

import json
import re

from app.review.domain import DependencyRef
from app.review.evidence import EvidenceArchive, SourceReadError, digest, source_lines
from app.review.tool_scope import is_sensitive_repository_path
from app.services.review.evidence.file_context import _repository


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
