import json
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.agent import AgentRunner
from app.review.configuration import scale_for_plan
from app.review.domain import WorkItem
from app.review.orchestrator import ReviewOrchestrator, discovery_priority
from app.review.report import build_report
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import completion_gap, setup_source_run


class GapModel(ReviewModel):
    resolve_gap: bool = True
    reviews: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if str(messages[0].content).startswith("Summarize"):
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        self.reviews += 1
        packet = json.loads(messages[1].content)
        followup = packet.get("followup")
        handles = [source["source_ref"] for material in packet["materials"] for source in material["sources"]]
        gap = None if followup and self.resolve_gap else completion_gap(source_refs=handles[:1])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            tool_calls=[{"name": "UnitCompletion", "id": "result", "args": {"unresolved": gap}}]))])


async def test_automatic_gap_followup_closes_gap_and_keeps_original_receipts(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = GapModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.reviews == 2
    assert report.status == "completed" and report.unresolved_unit_count == 0
    original = next(work for work in report.units if work.kind == "review")
    followup = next(work for work in report.units if work.kind == "supplement")
    assert original.has_unresolved_investigation and not followup.has_unresolved_investigation
    assert followup.followup_of == original.id and followup.followup_number == 1
    assert store.get(run.id).hard_deadline == run.hard_deadline
    assert report.usage["model_calls"] == 3
    assert len([item for item in store.work_items(run.id) if item.kind == "supplement"]) == 1


async def test_no_progress_stops_followup_and_keeps_unresolved_separate(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    model = GapModel(resolve_gap=False)
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.reviews == 2
    assert report.status == "completed" and report.coverage == "complete"
    assert report.unresolved_unit_count == 1 and not report.findings


async def test_reopened_store_does_not_duplicate_followup_or_reset_usage(tmp_path):
    from app.review.budget import BudgetGate
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    await AgentRunner(GapModel(resolve_gap=False)).run(work, provider, BudgetGate(store, run, lease))
    assert store.enqueue_gap_followups(run.id, lease) == 1
    reopened = type(store)(store.sessions)
    assert reopened.enqueue_gap_followups(run.id, lease) == 0
    followup = next(item for item in reopened.work_items(run.id) if item.kind == "supplement")
    assert reopened.claim(run.id, followup.id, lease, check_budget=True)
    unknown = reopened.reserve(run.id, reopened.work_items(run.id)[1], 1000, lease)
    before = reopened.usage(run.id)
    reopened.begin(run.id, lease)
    assert reopened.usage(run.id) == before
    assert reopened.work_items(run.id)[1].id == followup.id
    assert unknown


async def test_submission_reserve_rejects_more_reads_but_keeps_candidate_delivery(tmp_path):
    from types import SimpleNamespace
    from app.review.budget import BudgetGate
    from app.review.context import AgentContext
    from app.review.domain import CandidateInput
    from app.review.tools import read_file, submit_candidate
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease))
    initial = await provider.initial(context)
    await context.acknowledge(dict(context.pending), dict(context.pending_hunks))
    context.tool_calls = 77
    runtime = SimpleNamespace(context=context, tool_call_id="final-candidate")
    denied = await read_file.coroutine(path="a.py", revision="head", start=1, end=2, runtime=runtime)
    assert denied["error"] == "investigation_closed" and context.tool_calls == 77
    handle = next(source["source_ref"] for material in initial["materials"] for source in material["sources"]
                  if source["source"]["side"] == "head")
    receipt = await submit_candidate.coroutine(proposal=CandidateInput(claim="除零", trigger="零输入",
        impact="异常", source_refs=[handle]), runtime=runtime)
    assert receipt["ok"] and context.tool_calls == 78
    assert len(store.candidates(run.id)) == 1


def test_material_coverage_does_not_hide_failed_execution(tmp_path):
    _, run, _, _, _ = setup_source_run(tmp_path)
    work = WorkItem(id="work", kind="review", unit_id=run.plan.units[0].id,
        status="interrupted", material_delivery="complete", error_code="investigation_deadline")
    report = build_report(run, [work], [], {})
    assert report.status == "partial" and report.coverage == "complete"


def test_priority_places_auth_code_ahead_of_tests_and_changesets(tmp_path):
    _, run, _, _, _ = setup_source_run(tmp_path)
    unit = run.plan.units[0]
    auth = unit.model_copy(update={"paths": ["src/auth/callback.ts"], "risk": "high"})
    tests = unit.model_copy(update={"paths": ["tests/auth.test.ts"], "risk": "high"})
    docs = unit.model_copy(update={"paths": [".changeset/new.md"], "risk": "high"})
    assert discovery_priority(auth, 12) < discovery_priority(tests, 0)
    assert discovery_priority(auth, 12) < discovery_priority(docs, 1)


def test_default_budget_scales_but_explicit_deadline_stays_fixed(tmp_path):
    _, run, _, _, _ = setup_source_run(tmp_path)
    run.plan.units *= 25
    limits = run.configuration.options.limits
    limits.timeout_seconds = 1800
    scale_for_plan(run.configuration, run.plan, {"timeout_seconds": 1800})
    assert limits.timeout_seconds == 1800
    scale_for_plan(run.configuration, run.plan, {})
    assert 1800 < limits.timeout_seconds <= 7200
    assert limits.total_model_calls == 20


def test_completed_gap_cannot_produce_clean_publication(tmp_path):
    from app.services.publication.publication_planner import PublicationPlanner
    _, run, _, provider, _ = setup_source_run(tmp_path)
    work = WorkItem(id="work", kind="review", unit_id=run.plan.units[0].id,
        status="completed", material_delivery="complete", result={"unresolved": completion_gap()})
    run.report = build_report(run, [work], [], {})
    run.status = run.report.status
    plan = PublicationPlanner(provider.evidence.artifacts).build(run)
    assert plan.outcome.kind == "inconclusive"
    assert plan.outcome.coverage == "complete" and plan.outcome.unresolved_count == 1
