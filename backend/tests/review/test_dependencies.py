"""Only retained dependency evidence remains supported; acquisition is retired."""

import json

import pytest

from app.review.domain import Candidate, CandidateInput, DependencyRef, UnitCompletion, VerificationOutput
from app.review.evidence import digest
from app.review.findings import finding_digest
from app.services.publication.review_publication import _valid_dependencies
from tests.review.vnext_helpers import setup_source_run


async def test_retained_dependency_refutation_still_requires_archived_evidence(tmp_path):
    _, run, _, provider, _ = setup_source_run(tmp_path, support_sources={
        "go.mod": "module local\nrequire github.com/o/lib v1.2.3\n"})
    manifest = await provider.evidence.read("go.mod", 1, 2, "head")
    from app.review.domain import SourceRef
    manifest_ref = SourceRef.model_validate(manifest["evidence"])
    binding = {"kind": "go_module_tag", "package": "github.com/o/lib", "version": "v1.2.3",
               "repository": "o/lib", "commit_sha": "f" * 40, "metadata_hash": "e" * 64}
    payload = dict(review_id=run.id, package="github.com/o/lib", version="v1.2.3", repository="o/lib",
        commit_sha="f" * 40, path="client.go", start_line=1, end_line=1,
        content_hash=digest("func Get() {}\n"), content="func Get() {}\n",
        manifest_source=manifest_ref.model_dump(), binding=binding)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    provider.evidence.artifacts.put_text(task_id=run.id, kind="dependency", content=encoded)
    ref = DependencyRef(id=digest(encoded), **{key: value for key, value in payload.items() if key != "content"})
    run.dependency_pins[digest(json.dumps([manifest_ref.id, ref.package, ref.version]))] = binding
    candidate = Candidate(id="old", unit_id=run.plan.units[0].id, attempt_id="old-attempt", tool_call_id="old-call",
        proposal=CandidateInput(claim="调用失败", trigger="输入", impact="异常", source_refs=["S1"], dependency_refs=["D1"]),
        sources=[], dependency_sources=[ref], verification_dependency_sources=[ref],
        verification=VerificationOutput(verdict="refuted", attribution="introduced", reason="固定依赖包含保护", dependency_refs=["D1"]))
    candidate.decision_digest = finding_digest(candidate, run.snapshot)
    assert _valid_dependencies(provider.evidence, candidate, run)
    candidate.verification_dependency_sources.clear()
    assert not _valid_dependencies(provider.evidence, candidate, run)
    candidate.verification_dependency_sources = [ref]
    archive = provider.evidence.artifacts.root / run.id / f"dependency-{ref.id}.txt"
    archive.write_text("{}")
    with pytest.raises(ValueError):
        _valid_dependencies(provider.evidence, candidate, run)


def test_active_model_schemas_do_not_advertise_dependency_handles():
    for schema in (CandidateInput, VerificationOutput, UnitCompletion):
        assert "dependency_refs" not in json.dumps(schema.model_json_schema())
