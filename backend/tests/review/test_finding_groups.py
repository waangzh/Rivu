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


def semantic_comparison(candidates, verdict="same"):
    from app.review.domain import FindingComparison

    return FindingComparison(
        candidate_ids=tuple(item.id for item in candidates),
        admission_digests=tuple(item.finding.admission_digest for item in candidates),
        verdict=verdict, reason="同一个缺失的零值保护导致两个入口失败。",
        source_ids=list({source.id for item in candidates for source in item.verification_sources}))


async def test_semantic_comparison_merges_paraphrases_and_preserves_severity_disagreement(occurrences):
    from app.review.deduplication import apply_deduplication
    from app.review.domain import FindingDeduplication

    run, store, context, candidates = occurrences
    second = candidates[1]
    output = second.verification.model_copy(deep=True)
    output.root_cause.claim = "零输入的分母保护被移除"
    output.root_cause.trigger = "调用 ratio 时传入 0"
    output.root_cause.impact = "调用方收到除零错误"
    output.severity = "medium"
    admit_verification(second, output, context)
    assert len(aggregate(candidates)) == 2
    before = deepcopy(candidates)
    result = FindingDeduplication(status="ready", comparisons=[semantic_comparison(candidates)])
    report = build_report(run, store.work_items(run.id), candidates, {})
    apply_deduplication(report, result)
    assert len(report.findings) == 1
    assert len(report.finding_groups[0].member_candidate_ids) == 2
    assert {item.finding.severity for item in report.candidates} == {"high", "medium"}
    assert report.candidates == before
    reopened = type(report).model_validate_json(report.model_dump_json())
    assert aggregate(reopened.candidates, reopened.finding_deduplication) == report.findings


@pytest.mark.parametrize("difference", [
    "uncertain", "distinct", "digest", "source", "location", "snapshot", "eligibility", "repeated",
])
async def test_semantic_comparison_does_not_hide_unproved_or_changed_members(occurrences, difference):
    from app.review.domain import FindingDeduplication

    _, _, _, candidates = occurrences
    candidates[1].verification.root_cause.claim = "独立描述"
    comparison = semantic_comparison(candidates)
    if difference in {"uncertain", "distinct"}:
        comparison.verdict = difference
    elif difference == "digest":
        comparison.admission_digests = ("stale", comparison.admission_digests[1])
    elif difference == "source":
        comparison.source_ids = ["f" * 64]
    elif difference == "location":
        comparison.source_ids = [ref.id for ref in candidates[0].verification_sources]
    elif difference == "snapshot":
        candidates[1].verification_sources = [
            source.model_copy(update={"commit_sha": "f" * 40})
            for source in candidates[1].verification_sources]
    elif difference == "eligibility":
        candidates[1].finding.publishable = False
    result = FindingDeduplication(status="ready", comparisons=[
        comparison, *([comparison] if difference == "repeated" else [])])
    assert len(aggregate(candidates, result)) == 2


async def test_semantic_groups_require_pairwise_agreement_without_transitive_closure(occurrences):
    from app.review.domain import FindingDeduplication

    _, _, _, candidates = occurrences
    third = candidates[1].model_copy(deep=True)
    third.id, third.finding.id, third.finding.candidate_id = "third", "finding-third", "third"
    candidates.append(third)
    for index, item in enumerate(candidates):
        item.verification.root_cause.claim = f"表述 {index}"
    result = FindingDeduplication(status="ready", comparisons=[
        semantic_comparison(candidates[:2]), semantic_comparison(candidates[1:]),
        semantic_comparison([candidates[0], third], "distinct")])
    groups = group_findings(candidates, result)
    assert [len(item.member_candidate_ids) for item in groups] == [2, 1]


class ComparisonModel:
    def __init__(self, *, fail=False, invalid=False):
        self.calls = 0
        self.fail, self.invalid = fail, invalid

    def bind_tools(self, tools, **kwargs):
        assert len(tools) == 1 and tools[0].__name__ == "FindingComparisonOutput"
        return self

    async def ainvoke(self, messages):
        import json
        from langchain_core.messages import AIMessage

        self.calls += 1
        if self.fail:
            raise ValueError("PRIVATE_PROVIDER_BODY")
        packet = json.loads(messages[1].content)
        assessments = [{
            "pair_index": item["pair_index"], "verdict": "same", "reason": "同一根因和修复。",
            "source_refs": ["S999"] if self.invalid else [
                source["source_ref"] for source in packet["source_materials"]],
        } for item in packet["pairs"]]
        return AIMessage(content="", tool_calls=[{
            "id": "comparison", "name": "FindingComparisonOutput", "args": {"assessments": assessments}}],
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})


@pytest.mark.parametrize("outcome", [
    "same", "invalid", "failed", "budget", "deadline", "cancelled", "lease", "timeout",
])
async def test_bounded_comparison_uses_existing_sources_and_original_request_ledger(
    occurrences, outcome, monkeypatch,
):
    import asyncio
    from datetime import timedelta

    from app.models.orm import ReviewTaskOrm
    from app.review.deduplication import deduplicate_findings
    from app.review.domain import utcnow

    run, store, context, candidates = occurrences
    candidates[1].verification.root_cause.claim = "同一除零机制的另一个描述"
    candidates[1].finding.admission_digest = finding_digest(candidates[1], run.snapshot)
    report = build_report(run, store.work_items(run.id), candidates, {})
    model = ComparisonModel(fail=outcome == "failed", invalid=outcome == "invalid")
    if outcome in {"lease", "timeout"}:
        from app.review.store import LeaseLost

        async def interrupted(messages):
            model.calls += 1
            raise LeaseLost("fixture") if outcome == "lease" else TimeoutError
        monkeypatch.setattr(model, "ainvoke", interrupted)
        if outcome == "lease":
            with pytest.raises(LeaseLost):
                await deduplicate_findings(report, context.provider, context.budget, model)
            return
    if outcome in {"budget", "deadline"}:
        if outcome == "budget":
            run.configuration.options.limits.total_model_calls = 3
        else:
            run.hard_deadline = utcnow() - timedelta(seconds=1)
        with store.sessions.begin() as session:
            session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    if outcome == "cancelled":
        async def cancelled(messages):
            raise asyncio.CancelledError
        monkeypatch.setattr(model, "ainvoke", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await deduplicate_findings(report, context.provider, context.budget, model)
        return
    result = await deduplicate_findings(report, context.provider, context.budget, model)
    assert len(aggregate(candidates, result)) == (1 if outcome == "same" else 2)
    assert model.calls == (0 if outcome in {"budget", "deadline"} else 1)
    assert store.usage(run.id)["model_calls"] == model.calls
    assert store.usage(run.id)["unknown_calls"] == (1 if outcome in {"failed", "timeout"} else 0)
    if outcome in {"budget", "deadline"}:
        assert result.status == "skipped"
    if outcome == "same":
        report.finding_deduplication = result
        await asyncio.to_thread(store.save_report, run.id, report, context.provider.lease)
        context.provider.run = store.get(run.id)
        reused = await deduplicate_findings(report, context.provider, context.budget, model)
        assert reused == result and model.calls == 1


async def test_started_comparison_is_not_replayed_after_worker_recovery(occurrences):
    from app.review.deduplication import deduplicate_findings

    run, store, context, candidates = occurrences
    candidates[1].verification.root_cause.claim = "改写的相同根因"
    candidates[1].finding.admission_digest = finding_digest(candidates[1], run.snapshot)
    report = build_report(run, store.work_items(run.id), candidates, {})
    ticket = store.reserve_deduplication(run.id, 100, context.provider.lease)
    assert ticket
    model = ComparisonModel()
    result = await deduplicate_findings(report, context.provider, context.budget, model)
    assert result.status == "skipped" and result.error_code == "deduplication_not_replayed"
    assert model.calls == 0 and store.usage(run.id)["unknown_calls"] == 1


async def test_insufficient_comparison_material_does_not_consume_a_request(occurrences, monkeypatch):
    from app.review import deduplication

    run, store, context, candidates = occurrences
    candidates[1].verification.root_cause.claim = "根因的不同表述"
    candidates[1].finding.admission_digest = finding_digest(candidates[1], run.snapshot)
    monkeypatch.setattr(deduplication, "MAX_INPUT", 1)
    model = ComparisonModel()
    report = build_report(run, store.work_items(run.id), candidates, {})
    result = await deduplication.deduplicate_findings(report, context.provider, context.budget, model)
    assert result.status == "skipped" and result.omitted_pairs == 1
    assert model.calls == store.usage(run.id)["model_calls"] == 0
    assert len(aggregate(candidates, result)) == 2


async def test_duplicate_assessment_indices_cannot_suppress_members(occurrences):
    from langchain_core.messages import AIMessage
    from app.review.deduplication import admit_comparisons, comparison_material

    run, store, context, candidates = occurrences
    candidates[1].verification.root_cause.claim = "改写根因"
    candidates[1].finding.admission_digest = finding_digest(candidates[1], run.snapshot)
    report = build_report(run, store.work_items(run.id), candidates, {})
    packet, refs, pairs, _ = comparison_material(report, context.provider)
    assessment = {"pair_index": 0, "verdict": "same", "reason": "重复的判断。",
                  "source_refs": [item["source_ref"] for item in packet["source_materials"]]}
    raw = AIMessage(content="", tool_calls=[{"id": "comparison", "name": "FindingComparisonOutput",
        "args": {"assessments": [assessment, assessment]}}])
    assert admit_comparisons(raw, pairs, refs) == []
