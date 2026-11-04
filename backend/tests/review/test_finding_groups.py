from copy import deepcopy

import pytest

from app.review.budget import BudgetGate
from app.review.context import AgentContext
from app.review.domain import CandidateInput, Location, RootCause, VerificationOutput
from app.review.findings import (
    admit_candidate, admit_verification, aggregate, finding_digest, group_findings,
)
from app.review.report import build_report
from tests.review.vnext_helpers import setup_source_run


@pytest.fixture
async def occurrences(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "first.py": "first = ratio(0)\n", "second.py": "second = ratio(0)\n",
    })
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease))
    await provider.initial(context)
    root = RootCause(claim="ratio 移除了非零回退", trigger="x=0", impact="除零异常",
                     location=Location(path="a.py", line=2))
    candidates = []
    for path in ("first.py", "second.py"):
        source = await context.read(path, 1, 1, "head")
        await context.acknowledge(dict(context.pending), dict(context.pending_hunks))
        handles = [handle for handle, ref in context.visible.items() if ref.path == "a.py"]
        handles.append(source["source_ref"])
        proposal = CandidateInput(claim=f"{path} 的零输入调用抛出异常", trigger=f"调用 {path} 的入口",
            impact=f"{path} 入口失败", source_refs=handles,
            location=Location(path=path, line=1), root_cause=root.model_copy(deep=True))
        candidate = admit_candidate(proposal, context, path)
        output = VerificationOutput(verdict="confirmed", attribution="introduced", severity="high",
            reason="BASE 对零输入回退，HEAD 除零，当前调用传入零。",
            source_refs=handles, author_comment=f"修复 {path} 中调用到的非零回退。",
            root_cause=root.model_copy(deep=True))
        candidates.append(admit_verification(candidate, output, context))
    return run, store, context, candidates


async def test_same_root_across_files_keeps_each_independently_verified_occurrence(occurrences):
    run, store, _, candidates = occurrences
    assert all(candidate.finding for candidate in candidates)
    before = deepcopy(candidates)
    report = build_report(run, store.work_items(run.id), candidates, {})
    assert len(report.findings) == len(report.finding_groups) == 1
    group = report.finding_groups[0]
    assert group.representative_id == candidates[0].finding.id
    assert set(group.member_candidate_ids) == {item.id for item in candidates}
    assert {item.finding.location.path for item in report.candidates} == {"first.py", "second.py"}
    assert all(any(source.path == item.proposal.location.path
                   for source in item.finding.sources) for item in report.candidates)
    assert report.candidates == before
    rebuilt = type(report).model_validate_json(report.model_dump_json())
    assert rebuilt.finding_groups == report.finding_groups
    assert rebuilt.candidates == report.candidates


@pytest.mark.parametrize("difference", [
    "root_claim", "root_trigger", "root_impact", "root_line", "root_side",
    "severity", "attribution", "publishable", "snapshot", "unverified_root",
])
async def test_similar_occurrences_remain_separate_when_causal_identity_differs(occurrences, difference):
    _, _, _, candidates = occurrences
    second = candidates[1]
    root = second.verification.root_cause
    if difference.startswith("root_"):
        field = difference.removeprefix("root_")
        if field in {"line", "side"}:
            setattr(root.location, field, 1 if field == "line" else "base")
        else:
            setattr(root, field, "另一个可独立修复的失败")
    elif difference in {"severity", "attribution", "publishable"}:
        setattr(second.finding, difference, {
            "severity": "low", "attribution": "pre_existing", "publishable": False,
        }[difference])
    elif difference == "snapshot":
        second.verification_sources = [source.model_copy(update={"commit_sha": "f" * 40})
                                       for source in second.verification_sources]
    else:
        second.verification.root_cause = None
    assert len(aggregate(candidates)) == 2
    assert all(len(group.member_candidate_ids) == 1 for group in group_findings(candidates))


@pytest.mark.parametrize("verdict", ["insufficient", "refuted", "pending", "admission_failure"])
async def test_unconfirmed_occurrence_cannot_inherit_a_sibling_verdict(occurrences, verdict):
    _, _, context, candidates = occurrences
    second = candidates[1]
    output = second.verification.model_copy(update={
        "verdict": "confirmed" if verdict == "admission_failure" else
                   "insufficient" if verdict == "pending" else verdict,
        "source_refs": ["S999"] if verdict == "admission_failure" else second.verification.source_refs,
    })
    admit_verification(second, output, context)
    if verdict == "pending":
        second.verification = None
    assert second.finding is None
    assert aggregate(candidates) == [candidates[0].finding]
    assert group_findings(candidates)[0].member_candidate_ids == [candidates[0].id]


async def test_proposal_alone_cannot_establish_shared_root(occurrences):
    _, _, context, candidates = occurrences
    for candidate in candidates:
        output = candidate.verification.model_copy(update={"root_cause": None})
        admit_verification(candidate, output, context)
    assert all(candidate.proposal.root_cause for candidate in candidates)
    assert len(aggregate(candidates)) == 2


async def test_unsourced_root_is_rejected_at_both_admissions(occurrences):
    _, _, context, candidates = occurrences
    proposal = candidates[0].proposal.model_copy(deep=True)
    proposal.root_cause.location.line = 999
    with pytest.raises(ValueError, match="root_cause_not_sourced"):
        admit_candidate(proposal, context, "invalid-root")
    output = candidates[0].verification.model_copy(deep=True)
    output.root_cause.location.line = 999
    result = admit_verification(candidates[0], output, context)
    assert result.admission_reason == "root_cause_not_sourced"
    assert result.verification.verdict == "insufficient"
    assert result.finding is None
    assert aggregate(candidates) == [candidates[1].finding]


async def test_root_identity_is_bound_into_admission_digest(occurrences):
    run, _, _, candidates = occurrences
    candidate = candidates[0]
    original = candidate.finding.admission_digest
    candidate.verification.root_cause = candidate.verification.root_cause.model_copy(
        update={"trigger": "另一个输入"})
    assert finding_digest(candidate, run.snapshot) != original


async def test_pre_existing_group_keeps_default_publication_ineligible(occurrences):
    _, _, context, candidates = occurrences
    for candidate in candidates:
        admit_verification(candidate, candidate.verification.model_copy(
            update={"attribution": "pre_existing"}), context)
    findings = aggregate(candidates)
    assert len(findings) == 1
    assert findings[0].attribution == "pre_existing"
    assert all(not candidate.finding.publishable for candidate in candidates)


async def test_group_representative_prefers_a_head_location_without_losing_base_occurrence(occurrences):
    _, _, context, candidates = occurrences
    candidates[0].proposal.location = Location(path="a.py", line=2, side="base")
    admit_verification(candidates[0], candidates[0].verification, context)
    assert aggregate(candidates) == [candidates[1].finding]
    assert group_findings(candidates)[0].member_candidate_ids == [item.id for item in candidates]


async def test_unsourced_occurrence_location_cannot_use_shared_root_to_merge(occurrences):
    _, _, _, candidates = occurrences
    second = candidates[1]
    second.verification_sources = [source for source in second.verification_sources
                                   if source.path != "second.py"]
    assert len(aggregate(candidates)) == 2


async def test_exact_duplicate_fallback_keeps_all_candidate_ids(occurrences):
    _, _, _, candidates = occurrences
    first = candidates[0]
    first.verification.root_cause = None
    second = first.model_copy(deep=True)
    second.id, second.finding.id, second.finding.candidate_id = "duplicate", "finding-duplicate", "duplicate"
    assert aggregate([first, second]) == [first.finding]
    assert group_findings([first, second])[0].member_candidate_ids == [first.id, second.id]


async def test_reviewer_inventory_includes_other_units_without_granting_their_source_handles(
    occurrences, monkeypatch,
):
    _, store, context, candidates = occurrences
    other = candidates[1]
    other.unit_id = "another-unit"
    monkeypatch.setattr(store, "candidates", lambda run_id: candidates)
    fresh = AgentContext(context.provider, context.work, context.budget)
    packet = await context.provider.initial(fresh)
    entry = next(item for item in packet["already_submitted"] if item["id"] == other.id)
    assert entry["root_cause"] == other.verification.root_cause.model_dump()
    assert entry["trigger"] == other.proposal.trigger
    assert "source_refs" not in entry
    assert not fresh.visible
    assert all(source.path != "second.py" for source in fresh.pending.values())
