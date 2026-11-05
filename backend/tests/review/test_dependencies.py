import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.review.budget import BudgetGate
from app.review.context import AgentContext
from app.review.dependencies import load_dependency, pinned_dependency, read_dependency
from app.review.domain import CandidateInput
from app.review.evidence import SourceReadError
from app.review.findings import admit_candidate
from tests.review.vnext_helpers import setup_source_run


def test_only_fixed_supported_manifest_versions_are_resolved():
    assert pinned_dependency("go.mod", "module local\nrequire github.com/cli/go-gh/v2 v2.13.0\n",
        "github.com/cli/go-gh/v2") == ("v2.13.0", "cli/go-gh")
    with pytest.raises(SourceReadError, match="dependency_replace_unsupported"):
        pinned_dependency("go.mod", "require github.com/o/r v1.0.0\nreplace github.com/o/r => ../r\n",
                          "github.com/o/r")
    with pytest.raises(SourceReadError, match="dependency_version_form_unsupported"):
        pinned_dependency("go.mod", "require github.com/o/r v0.0.0-20250101120000-abcdefabcdef\n",
                          "github.com/o/r")
    with pytest.raises(SourceReadError, match="dependency_version_not_pinned"):
        pinned_dependency("package-lock.json", '{"packages":{"node_modules/demo":{"version":"^1.0.0"}}}', "demo")


async def dependency_fixture(tmp_path, monkeypatch, content=b"func Get() {}\n"):
    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "go.mod": "module local\nrequire github.com/o/lib v1.2.3\n"})
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease))
    requests = []
    sha = "f" * 40
    blobs = {"client.go": content, "go.mod": b"module github.com/o/lib\n"}
    blob_ids = {path: hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
                for path, content in blobs.items()}

    def transport(request):
        requests.append(str(request.url))
        path = request.url.path
        if "/git/ref/tags/" in path:
            return httpx.Response(200, json={"object": {"type": "commit", "sha": sha}})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": "e" * 40}})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": name, "type": "blob", "mode": "100644", "size": len(content), "sha": blob_ids[name]}
                for name, content in blobs.items()]})
        content = next(content for name, content in blobs.items() if path.endswith(blob_ids[name]))
        return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(content).decode()})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        original_client(transport=httpx.MockTransport(transport), **kwargs))
    ref, content = await load_dependency(context, "go.mod", "github.com/o/lib", "client.go", 1, 1)
    return store, run, lease, provider, context, ref, content, requests


async def test_dependency_preserves_final_blank_line(tmp_path, monkeypatch):
    _, _, _, provider, context, _, _, _ = await dependency_fixture(tmp_path, monkeypatch, b"func Get() {}\n\n")
    ref, content = await load_dependency(context, "go.mod", "github.com/o/lib", "client.go", 1, 2)
    assert content == "func Get() {}\n\n"
    assert read_dependency(provider.evidence, ref) == content


async def test_removed_dependency_list_cannot_validate_refutation(tmp_path, monkeypatch):
    from app.review.domain import VerificationOutput
    from app.review.findings import admit_verification
    from app.services.publication.review_publication import _valid_dependencies
    _, run, _, provider, context, ref, _, _ = await dependency_fixture(tmp_path, monkeypatch)
    handle = context.stage_dependency(ref)
    target = await context.read("a.py", 1, 2, "head")
    await context.acknowledge(dict(context.pending), {}, dict(context.pending_dependencies))
    candidate = admit_candidate(CandidateInput(claim="失败", trigger="输入", impact="异常",
        source_refs=[target["source_ref"]], dependency_refs=[handle]), context, "candidate")
    output = VerificationOutput(verdict="refuted", attribution="introduced", reason="固定依赖有保护",
        source_refs=[target["source_ref"]], dependency_refs=[handle])
    candidate = admit_verification(candidate, output, context)
    run = provider.store.get(run.id)
    assert _valid_dependencies(provider.evidence, candidate, run)
    candidate.verification_dependency_sources.clear()
    assert not _valid_dependencies(provider.evidence, candidate, run)


async def test_pending_dependency_body_survives_context_rebuild(tmp_path, monkeypatch):
    import json
    from tests.review.test_working_context import scenario, estimate
    _, _, _, _, _, ref, content, _ = await dependency_fixture(tmp_path, monkeypatch)
    context, messages = scenario()
    handle = context.stage_dependency(ref)
    body = {"dependency_ref": handle, "dependency": ref.model_dump(), "content": content}
    context.initial_packet["dependency_materials"] = [body]
    messages[0].content = json.dumps(context.initial_packet)
    rebuilt, _, _ = context.window.rebuild(context, messages, estimate, 20_000, 40_000)
    assert context.dependency_bodies(rebuilt)[handle] == body
    assert handle in context.pending_dependencies and not context.dependencies


async def test_parallel_dependency_reads_respect_material_slots(tmp_path, monkeypatch):
    import asyncio
    from app.review.tools import read_dependency_source
    _, _, _, _, context, ref, content, _ = await dependency_fixture(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def waiting_load(*args):
        entered.set()
        await release.wait()
        return ref, content
    monkeypatch.setattr("app.review.dependencies.load_dependency", waiting_load)
    context.dependency_slots = 11
    runtime = SimpleNamespace(context=context)
    first = asyncio.create_task(read_dependency_source.coroutine(manifest_path="go.mod",
        package="github.com/o/lib", path="client.go", start=1, end=1, runtime=runtime))
    await entered.wait()
    denied = await read_dependency_source.coroutine(manifest_path="go.mod", package="github.com/o/lib",
        path="other.go", start=1, end=1, runtime=runtime)
    assert denied["error"] == "dependency_material_limit"
    release.set()
    assert (await first)["ok"] and context.dependency_slots == 11


async def test_dependency_version_freezes_and_independent_context_gets_archived_body(tmp_path, monkeypatch):
    store, run, lease, provider, context, ref, content, requests = await dependency_fixture(tmp_path, monkeypatch)
    assert ref.version == "v1.2.3" and ref.commit_sha == "f" * 40
    assert read_dependency(provider.evidence, ref) == content
    handle = context.stage_dependency(ref)
    with pytest.raises(ValueError, match="dependency_reference_not_received"):
        context.resolve_dependencies([handle])
    await context.acknowledge({}, {}, dict(context.pending_dependencies))
    target = await context.read("a.py", 1, 2, "head")
    await context.acknowledge(dict(context.pending), {})
    proposal = CandidateInput(claim="调用依赖", trigger="输入", impact="结果", source_refs=[target["source_ref"]],
                              dependency_refs=[handle])
    candidate = admit_candidate(proposal, context, "candidate")
    verifier = AgentContext(provider, context.work, context.budget)
    initial = await provider.initial(verifier, candidate)
    assert initial["dependency_materials"][0]["content"] == content
    assert not verifier.dependencies and verifier.pending_dependencies
    await verifier.acknowledge({}, {}, dict(verifier.pending_dependencies))
    assert verifier.resolve_dependencies([initial["dependency_materials"][0]["dependency_ref"]]) == [ref]
    with pytest.raises(ValueError, match="source_not_visible"):
        verifier.resolve(["D1"])
    calls = len([url for url in requests if "/git/ref/" in url])
    loaded, _ = await load_dependency(context, "go.mod", "github.com/o/lib", "client.go", 1, 1)
    assert loaded == ref
    assert len([url for url in requests if "/git/ref/" in url]) == calls
    assert len(store.get(run.id).dependency_pins) == 1
    archive_path = provider.evidence.artifacts.root / run.id / f"dependency-{ref.id}.txt"
    archive_path.write_text("{}")
    with pytest.raises(ValueError):
        read_dependency(provider.evidence, ref)


async def test_missing_dependency_source_remains_an_explicit_gap(tmp_path, monkeypatch):
    from app.review.tools import read_dependency_source
    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "go.mod": "module local\nrequire github.com/o/lib v1.2.3\n"})
    context = AgentContext(provider, store.claim(run.id, store.work_items(run.id)[0].id, lease),
                           BudgetGate(store, run, lease))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404)), **kwargs))
    result = await read_dependency_source.coroutine(manifest_path="go.mod", package="github.com/o/lib",
        path="client.go", start=1, end=1, runtime=SimpleNamespace(context=context))
    assert result["error"] == "dependency_reference_unavailable"
    assert not store.candidates(run.id) and not context.dependencies and not context.pending_dependencies
