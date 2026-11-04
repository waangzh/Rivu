import json
import logging

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.agent import AgentRunner
from app.review.orchestrator import ReviewOrchestrator
from tests.review.vnext_helpers import setup_source_run


class ReviewModel(BaseChatModel):
    fail_after_submit: bool = False
    empty: bool = False
    overview_failure: bool = False
    unknown_usage: bool = False
    invalid_verification_refs: bool = False

    @property
    def _llm_type(self):
        return "review-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = str(messages[0].content)
        packet = json.loads(messages[1].content)
        tools = [message for message in messages if isinstance(message, ToolMessage)]
        if system.startswith("Summarize"):
            if self.overview_failure:
                raise ValueError("fixture_failure")
            name = "OverviewOutput"
            payload = {"change_overview": [{"text": "更新比例计算逻辑。",
                "source_refs": [packet["source_materials"][0]["source_ref"]]}], "finding_order": []}
        elif system.startswith("Independently"):
            handles = [source["source_ref"] for material in packet["materials"] for source in material["sources"]]
            if self.invalid_verification_refs:
                handles = ["S999"]
            name = "VerificationOutput"
            payload = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                "reason": "零输入现在到达除零路径，BASE 使用非零回退。", "source_refs": handles,
                "author_comment": "传入零时会除零并中断请求。恢复非零回退或显式处理零输入。"}
        elif tools or self.empty:
            if tools and self.fail_after_submit:
                raise ValueError("reviewer_crashed")
            name, payload = "UnitCompletion", {}
        else:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            name = "submit_candidate"
            payload = {"proposal": {"claim": "零输入导致除零", "trigger": "x=0", "impact": "请求失败",
                "source_refs": [handle], "location": {"path": "a.py", "line": 2, "side": "head"}}}
        usage = None if self.unknown_usage else {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", usage_metadata=usage,
            tool_calls=[{"name": name, "args": payload, "id": f"tool-{len(tools)}"}]))])


class ReferenceCorrectionModel(ReviewModel):
    corrected_verdict: str = "confirmed"
    correction_failure: bool = False
    correction_reads: bool = False
    correction_count: int = 0
    missing_sources: bool = False
    missing_attribution_sources: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not str(messages[0].content).startswith("Independently"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        correcting = any(str(message.content).startswith("Runtime correction") for message in messages)
        if correcting:
            self.correction_count += 1
            assert self.correction_count == 1
            if self.correction_failure:
                raise ValueError("PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE")
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        call = result.generations[0].message.tool_calls[0]
        if not correcting:
            if self.missing_attribution_sources:
                packet = json.loads(messages[1].content)
                call["args"]["source_refs"] = [
                    source["source_ref"] for material in packet["materials"]
                    for source in material["sources"] if source["source"]["side"] == "head"]
            elif self.missing_sources:
                call["args"]["source_refs"] = []
            else:
                call["args"]["source_refs"].append("R1")
        elif self.correction_reads:
            call.update(name="read_file", args={"path": "a.py", "revision": "head", "start": 1, "end": 2})
        else:
            call["args"]["verdict"] = self.corrected_verdict
            call["args"]["verified_claim"] = {
                "claim": "零输入导致函数除零", "trigger": "直接传入 x=0", "impact": "函数抛出除零异常",
            } if self.corrected_verdict == "confirmed" else None
            if self.corrected_verdict == "insufficient":
                call["args"]["question"] = {
                    "observation": "函数直接以 x 为除数。",
                    "missing_premise": "尚未确定接口是否允许零输入。",
                    "question": "请确认 x=0 是否为允许输入。",
                }
        return result


class QuestionReferenceModel(ReferenceCorrectionModel):
    corrected_verdict: str = "insufficient"
    missing_after_correction: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if str(messages[0].content).startswith("Independently"):
            payload = result.generations[0].message.tool_calls[0]["args"]
            payload.update(verdict="insufficient", severity=None, author_comment=None,
                verified_claim=None, question={
                    "observation": "函数直接以 x 为除数。",
                    "missing_premise": "尚未确定接口是否允许零输入。",
                    "question": "请确认接口是否允许零输入。",
                })
            if self.missing_after_correction:
                payload["source_refs"] = []
        return result


class SharedRootModel(ReviewModel):
    reject_second: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        call = message.tool_calls[0]
        root = {"claim": "ratio 移除了非零回退", "trigger": "x=0", "impact": "除零异常",
                "location": {"path": "a.py", "line": 2, "side": "head"}}
        if call["name"] == "submit_candidate":
            proposal = call["args"]["proposal"]
            proposal["root_cause"] = root
            message.tool_calls.extend([
                {"name": "submit_candidate", "id": "second-location", "type": "tool_call",
                 "args": {"proposal": {
                    **proposal, "claim": "另一位置的相同零输入失败",
                    "location": {"path": "a.py", "line": 1, "side": "head"},
                }}},
            ])
        elif call["name"] == "VerificationOutput":
            packet = json.loads(messages[1].content)
            if self.reject_second and packet["candidate"]["claim"].startswith("另一位置"):
                call["args"].update(verdict="insufficient", severity=None, author_comment=None,
                                    reason="这一位置的入口契约尚未确定。")
            else:
                call["args"]["root_cause"] = root
        return result


class ParaphrasedRootModel(SharedRootModel):
    comparison_calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if str(messages[0].content).startswith("Compare suspected"):
            self.comparison_calls += 1
            packet = json.loads(messages[1].content)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                tool_calls=[{"id": "comparison", "name": "FindingComparisonOutput", "args": {
                    "assessments": [{"pair_index": pair["pair_index"], "verdict": "same",
                        "reason": "两个位置都因相同的零值保护缺失而失败。",
                        "source_refs": [source["source_ref"]
                                        for source in packet["source_materials"]]}
                        for pair in packet["pairs"]],
                }}]))])
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        call = result.generations[0].message.tool_calls[0]
        if (call["name"] == "VerificationOutput"
                and json.loads(messages[1].content)["candidate"]["claim"].startswith("另一位置")):
            call["args"]["root_cause"].update(
                claim="零输入的分母保护已移除", trigger="参数为零", impact="直接除零抛错")
            call["args"]["severity"] = "medium"
        return result


async def test_semantic_comparison_groups_independent_verifications_and_publisher_rechecks_members(
    tmp_path,
):
    from app.services.publication.publication_planner import PublicationPlanner

    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = ParaphrasedRootModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(report.candidates) == 2 and len(report.findings) == 1
    assert report.finding_deduplication.status == "ready"
    assert len(report.finding_deduplication.comparisons) == 1 and model.comparison_calls == 1
    assert {item.finding.severity for item in report.candidates} == {"high", "medium"}
    assert report.usage["model_calls"] == 6
    saved = store.get(run.id)
    plan = PublicationPlanner(provider.evidence.artifacts).build(saved)
    assert len(plan.inline_drafts) == 1 and not plan.withheld_findings
    hidden = next(item for item in saved.report.candidates
                  if item.id != saved.report.findings[0].candidate_id)
    hidden.verification_sources = []
    changed = PublicationPlanner(provider.evidence.artifacts).build(saved)
    assert any(item.candidate_id == hidden.id for item in changed.withheld_findings)


@pytest.mark.parametrize("reject_second", [False, True])
async def test_root_grouping_survives_concurrent_verification_and_report_reopen(tmp_path, reject_second):
    from app.services.publication.publication_planner import PublicationPlanner

    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(SharedRootModel(
        reject_second=reject_second))).run(run.id, provider, lease)
    assert len(report.candidates) == 2
    assert len([item for item in report.units if item.kind == "verification"
                and item.status == "completed"]) == 2
    assert len(report.findings) == len(report.finding_groups) == 1
    assert len(report.finding_groups[0].member_candidate_ids) == (1 if reject_second else 2)
    assert report.usage["model_calls"] == 5
    saved = store.get(run.id)
    assert saved.report.candidates == report.candidates
    assert saved.report.finding_groups == report.finding_groups
    plan = PublicationPlanner(provider.evidence.artifacts).build(saved)
    assert len(plan.inline_drafts) == 1
    assert not plan.withheld_findings
    assert plan.outcome.unresolved_count == (1 if reject_second else 0)


@pytest.mark.parametrize("missing_sources", [False, True])
async def test_unsourced_question_is_corrected_and_reaches_final_comment(tmp_path, missing_sources):
    from app.review.findings import finding_digest
    from app.services.publication.publication_planner import PublicationPlanner

    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = QuestionReferenceModel(missing_sources=missing_sources)
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    candidate = report.candidates[0]
    assert model.correction_count == 1
    assert report.usage["model_calls"] == 5
    assert candidate.verification.verdict == "insufficient"
    assert candidate.admission_reason is None
    assert candidate.verification_sources
    assert candidate.question_digest == finding_digest(candidate, run.snapshot)
    assert candidate.finding is None and report.findings == []
    saved = store.get(run.id)
    plan = PublicationPlanner(provider.evidence.artifacts).build(saved)
    assert plan.outcome.unresolved_count == 1
    assert plan.outcome.publishable_count == plan.outcome.confirmed_count == 0
    assert len(plan.unresolved_questions) == 1
    assert candidate.verification.question.question in plan.review_comment_markdown
    assert "未计入已确认问题" in plan.review_comment_markdown
    assert plan.inline_drafts == plan.summary_findings == []


@pytest.mark.parametrize("budget_limited", [False, True])
async def test_question_stays_withheld_when_reference_correction_cannot_succeed(tmp_path, budget_limited):
    from app.models.orm import ReviewTaskOrm
    from app.services.publication.publication_planner import PublicationPlanner

    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    if budget_limited:
        run.configuration.options.limits.total_model_calls = 4
        with sessions.begin() as session:
            session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    model = QuestionReferenceModel(missing_sources=True, missing_after_correction=True)
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    candidate = report.candidates[0]
    assert model.correction_count == (0 if budget_limited else 1)
    assert report.usage["model_calls"] == (4 if budget_limited else 5)
    assert candidate.verification.verdict == "insufficient"
    assert candidate.admission_reason == "missing_decision_sources"
    assert candidate.question_digest is None
    assert candidate.verification_sources == []
    assert report.findings == []
    plan = PublicationPlanner(provider.evidence.artifacts).build(store.get(run.id))
    assert plan.unresolved_questions == []
    assert plan.outcome.unresolved_count == 1


async def test_insufficient_without_question_does_not_require_decision_sources(tmp_path):
    class InsufficientModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            if str(messages[0].content).startswith("Independently"):
                result.generations[0].message.tool_calls[0]["args"].update(
                    verdict="insufficient", source_refs=[], question=None, severity=None, author_comment=None)
            return result

    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(InsufficientModel())).run(run.id, provider, lease)
    candidate = report.candidates[0]
    assert report.usage["model_calls"] == 4
    assert candidate.verification.verdict == "insufficient"
    assert candidate.admission_reason is None
    assert candidate.question_digest is None
    assert candidate.verification_sources == []
    assert report.findings == []


async def test_actual_agent_loop_candidate_verifier_and_default_overview(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "completed", report
    assert len(report.findings) == 1
    assert report.findings[0].publishable
    assert report.overview.status == "ready", report.overview
    assert report.usage["model_calls"] == 4
    assert all(item.material_delivery == "complete" for item in report.units)
    assert store.get(run.id).report == report


async def test_invalid_verification_sources_are_admission_failure_not_execution_failure(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(
        ReviewModel(invalid_verification_refs=True))).run(run.id, provider, lease)
    assert report.status == "completed"
    assert report.findings == []
    assert "unverified_candidates" not in report.reasons
    candidate = report.candidates[0]
    assert candidate.verification.verdict == "insufficient"
    assert candidate.admission_reason == "invalid_source_refs"
    assert candidate.verification_sources == []
    assert candidate.finding is None
    assert report.usage["model_calls"] == 5


@pytest.mark.parametrize("verdict", ["confirmed", "refuted", "insufficient"])
@pytest.mark.parametrize("missing_sources", [False, True])
async def test_invalid_or_missing_sources_get_one_budgeted_correction(tmp_path, verdict, missing_sources):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = ReferenceCorrectionModel(corrected_verdict=verdict, missing_sources=missing_sources)
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    candidate = report.candidates[0]
    assert model.correction_count == 1
    assert report.usage["model_calls"] == 5
    assert report.usage["unknown_calls"] == 0
    assert candidate.verification.verdict == verdict
    assert candidate.admission_reason is None
    assert candidate.verification_sources
    work = next(item for item in report.units if item.kind == "verification")
    assert work.status == "completed" and work.attempt == 1
    assert len(report.findings) == (1 if verdict == "confirmed" else 0)
    if verdict == "confirmed":
        assert candidate.proposal.impact == "请求失败"
        assert report.findings[0].impact == "函数抛出除零异常"
    elif verdict == "insufficient":
        assert candidate.verification.question.missing_premise == "尚未确定接口是否允许零输入。"
        from app.review.findings import finding_digest
        assert candidate.question_digest == finding_digest(candidate, run.snapshot)


async def test_reference_correction_cannot_take_over_protected_overview_budget(tmp_path):
    from app.models.orm import ReviewTaskOrm

    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.total_model_calls = 4
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    model = ReferenceCorrectionModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.correction_count == 0
    assert report.usage["model_calls"] == 4
    assert report.overview.status == "ready"
    assert report.candidates[0].admission_reason == "invalid_source_refs"
    assert report.candidates[0].verification.verdict == "insufficient"
    assert report.findings == []


async def test_reference_correction_respects_agent_call_fuse(tmp_path):
    from app.models.orm import ReviewTaskOrm

    class CombinedModel(ReferenceCorrectionModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            if message.tool_calls[0]["name"] == "submit_candidate":
                message.tool_calls.append({"name": "UnitCompletion", "args": {}, "id": "completion"})
            return result

    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = 1
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    model = CombinedModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.correction_count == 0
    assert report.usage["model_calls"] == 3
    assert report.candidates[0].admission_reason == "invalid_source_refs"
    assert report.findings == []


@pytest.mark.parametrize("mode", ["failure", "reads"])
async def test_failed_correction_keeps_rejected_result_and_never_reinvestigates(tmp_path, caplog, mode):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = ReferenceCorrectionModel(correction_failure=mode == "failure", correction_reads=mode == "reads")
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.correction_count == 1
    assert report.usage["model_calls"] == 5
    assert report.usage["unknown_calls"] == (1 if mode == "failure" else 0)
    candidate = report.candidates[0]
    assert candidate.verification.verdict == "insufficient"
    assert candidate.admission_reason == "invalid_source_refs"
    assert candidate.finding is None
    work = next(item for item in report.units if item.kind == "verification")
    assert work.result["verdict"] == "confirmed"
    assert work.result["source_refs"][-1] == "R1"
    assert "PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE" not in caplog.text


@pytest.mark.parametrize("stop", ["cancelled", "lease_lost"])
@pytest.mark.parametrize("missing_attribution_sources", [False, True])
async def test_reference_correction_propagates_cancellation_and_lease_loss(
    tmp_path, stop, missing_attribution_sources,
):
    from app.models.orm import ReviewTaskOrm, WorkerJobOrm
    from app.review.store import LeaseLost, ReviewStopped

    store, run, lease, provider, sessions = setup_source_run(tmp_path)

    class StopDuringCorrectionModel(ReferenceCorrectionModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any(str(message.content).startswith("Runtime correction") for message in messages):
                with sessions.begin() as session:
                    if interruption == "cancelled":
                        session.get(ReviewTaskOrm, run.id).status = "cancelled"
                    else:
                        session.get(WorkerJobOrm, lease["job_id"]).lease_owner = "replacement-worker"
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    interruption = stop
    with pytest.raises(ReviewStopped if stop == "cancelled" else LeaseLost):
        await ReviewOrchestrator(store, AgentRunner(StopDuringCorrectionModel(
            missing_attribution_sources=missing_attribution_sources))).run(run.id, provider, lease)
    assert store.candidates(run.id)[0].verification is None
    assert all(item.status != "completed" for item in store.work_items(run.id)
               if item.kind == "verification")


@pytest.mark.parametrize("missing_attribution_sources", [False, True])
async def test_reference_correction_returning_after_deadline_cannot_save_a_finding(
    tmp_path, missing_attribution_sources,
):
    from datetime import timedelta
    from app.models.orm import ReviewTaskOrm
    from app.review.domain import utcnow

    store, run, lease, provider, sessions = setup_source_run(tmp_path)

    class LateCorrectionModel(ReferenceCorrectionModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if any(str(message.content).startswith("Runtime correction") for message in messages):
                provider.run.hard_deadline = utcnow() - timedelta(seconds=1)
                with sessions.begin() as session:
                    row = session.get(ReviewTaskOrm, run.id)
                    row.task_snapshot = {**row.task_snapshot,
                        "hard_deadline": provider.run.hard_deadline.isoformat()}
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    report = await ReviewOrchestrator(store, AgentRunner(LateCorrectionModel(
        missing_attribution_sources=missing_attribution_sources))).run(run.id, provider, lease)
    assert report.findings == []
    assert report.candidates[0].verification is None
    work = next(item for item in report.units if item.kind == "verification")
    assert work.status == "interrupted" and work.error_code == "hard_deadline"
    assert report.overview.status == "skipped_deadline"
    assert report.usage["model_calls"] == 4


def test_safe_error_detail_only_exposes_stable_codes():
    from app.review.agent import safe_error_detail
    from app.review.context import RepeatedToolLoop
    from app.review.store import BudgetExceeded

    assert safe_error_detail(ValueError("source_not_visible_in_this_attempt")) == (
        "source_not_visible_in_this_attempt")
    assert safe_error_detail(BudgetExceeded("input_token_limit")) == "input_token_limit"
    assert safe_error_detail(RepeatedToolLoop("agent_tool_loop")) == "agent_tool_loop"
    assert safe_error_detail(RuntimeError("source_not_visible_in_this_attempt")) == "-"
    assert safe_error_detail(BudgetExceeded("private_provider_body")) == "-"
    assert safe_error_detail(ValueError("private_provider_body_token_and_source")) == "-"
    assert safe_error_detail(ValueError("PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE")) == "-"
    assert safe_error_detail(ValueError("vendor body with spaces")) == "-"


async def test_candidate_and_completion_in_one_response_wait_for_saved_receipt(tmp_path):
    class CombinedModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            if message.tool_calls and message.tool_calls[0]["name"] == "submit_candidate":
                message.tool_calls.append({"name": "UnitCompletion", "args": {}, "id": "completion"})
            return result
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(CombinedModel())).run(run.id, provider, lease)
    assert len(report.candidates) == 1
    assert len(report.findings) == 1
    assert report.status == "completed"


async def test_three_calls_finish_combined_discovery_verification_and_overview(tmp_path):
    from app.models.orm import ReviewTaskOrm

    class CombinedModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            if message.tool_calls and message.tool_calls[0]["name"] == "submit_candidate":
                message.tool_calls.append({"name": "UnitCompletion", "args": {}, "id": "completion"})
            return result

    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.total_model_calls = 3
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    report = await ReviewOrchestrator(store, AgentRunner(CombinedModel())).run(run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    assert report.overview.status == "ready"
    assert report.usage["model_calls"] == 3


async def test_candidate_survives_reviewer_failure_and_is_verified(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(fail_after_submit=True))).run(
        run.id, provider, lease)
    assert report.status == "partial"
    assert len(report.findings) == 1
    assert report.overview.status == "ready"
    assert report.usage["unknown_calls"] == 1


async def test_normal_completion_without_material_is_partial(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    provider.evidence.max_file_bytes = 1
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(empty=True))).run(run.id, provider, lease)
    assert report.status == "partial"
    assert report.units[0].status == "completed"
    assert report.units[0].material_delivery == "incomplete"
    assert report.findings == []


async def test_overview_failure_keeps_completed_facts_and_unknown_usage(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(overview_failure=True,
        unknown_usage=True))).run(run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    assert report.overview.status == "failed"
    assert report.usage["input_tokens"] is None
    assert report.usage["committed_input_tokens"] > 0


@pytest.mark.parametrize("verdict", ["confirmed", "refuted"])
async def test_missing_decision_sources_cannot_admit_a_finding(tmp_path, verdict):
    from app.review.budget import BudgetGate
    from app.review.context import AgentContext
    from app.review.domain import Candidate, CandidateInput, VerificationOutput
    from app.review.findings import admit_verification
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease))
    candidate = Candidate(id="candidate", unit_id=work.unit_id, attempt_id=work.attempt_id,
        tool_call_id="call", proposal=CandidateInput(claim="bug", trigger="x", impact="y",
                                                    source_refs=["S1"]), sources=[])
    output = VerificationOutput(verdict=verdict, attribution="introduced", reason="claim")
    result = admit_verification(candidate, output, context)
    assert result.verification.verdict == "insufficient"
    assert result.finding is None


@pytest.mark.parametrize("private_body", [
    "PRIVATE_PROVIDER_BODY_TOKEN_AND_SOURCE",
    "private_provider_body_token_and_source",
])
async def test_model_failure_logs_unknown_usage_without_provider_exception(tmp_path, caplog, private_body):
    class PrivateFailureModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if str(messages[0].content).startswith("Investigate") and any(
                isinstance(message, ToolMessage) for message in messages
            ):
                raise ValueError(private_body)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    caplog.set_level(logging.INFO, logger="Rivu.Review")
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(PrivateFailureModel())).run(run.id, provider, lease)
    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name.startswith("Rivu.Review"))
    assert report.status == "partial"
    assert "模型请求未交付" in text
    assert "error_type=ValueError stage=execution response_received=False usage_known=False" in text
    assert "Agent未完成" in text
    assert private_body not in text
    assert "detail=-" in text
    assert report.usage["unknown_calls"] == 1
