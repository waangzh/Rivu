import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.domain import utcnow
from app.review.orchestrator import ReviewOrchestrator
from app.review.store import BudgetExceeded
from tests.review.test_gap_followups import GapModel
from tests.review.vnext_helpers import completion_gap, setup_run, setup_source_run, submit_candidate


def freeze_phases(sessions, run):
    now = utcnow()
    run.first_pass_deadline = now + timedelta(minutes=10)
    run.first_verification_deadline = now + timedelta(minutes=15)
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")


async def test_followup_starts_after_first_verification_and_new_candidate_is_verified(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path, calls=10, input_tokens=200_000, concurrency=1)
    run.configuration.options.generate_overview = False
    freeze_phases(sessions, run)
    order = []

    class Runner:
        model = None

        async def run(self, work, provider, budget):
            order.append((work.kind, work.followup_number))
            await budget.reserve(work, 1000)
            if work.kind != "verification":
                submit_candidate(store, run, lease, work)
            if work.kind == "verification" and not work.followup_number:
                # First verification ends after the first-pass deadline.
                run.first_pass_deadline = utcnow() - timedelta(seconds=1)
                with sessions.begin() as session:
                    session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
            result = {"unresolved": completion_gap()} if work.kind == "review" else {}
            await asyncio.to_thread(store.finish_work, run.id, work, "completed", result, None, lease)

    report = await ReviewOrchestrator(store, Runner()).run(run.id, SimpleNamespace(run=run), lease)
    assert order == [("review", 0), ("verification", 0), ("supplement", 1), ("verification", 1)]
    assert report.usage["model_calls"] == 4
    assert len(store.candidates(run.id)) == 2
    assert store.get(run.id).hard_deadline == run.hard_deadline


@pytest.mark.parametrize("resource", ["calls", "input", "output"])
def test_first_pass_leaves_shared_tail_and_supplement_candidate_verification(tmp_path, resource):
    store, run, lease, sessions = setup_run(tmp_path, calls=10, input_tokens=120_000)
    options, limits = run.configuration.options, run.configuration.options.limits
    options.generate_overview = False
    limits.total_model_calls = 10 if resource == "calls" else None
    limits.total_input_tokens = 120_000 if resource == "input" else None
    limits.total_output_tokens = 10 * limits.output_tokens if resource == "output" else None
    freeze_phases(sessions, run)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease, check_budget=True)
    request_input = 20_000 if resource == "input" else 1000
    used = 0
    while True:
        try:
            store.reserve(run.id, work, request_input, lease)
        except BudgetExceeded:
            break
        used += 1
    assert 0 < used < 10
    store.finish_work(run.id, work, "completed", {"unresolved": completion_gap()}, None, lease)
    assert store.enqueue_gap_followups(run.id, lease) == 1
    reopened = type(store)(sessions)
    followup = next(item for item in reopened.work_items(run.id) if item.kind == "supplement")
    followup = reopened.claim(run.id, followup.id, lease, check_budget=True)
    reopened.reserve(run.id, followup, 1000, lease)
    submit_candidate(reopened, run, lease, followup)
    reopened.finish_work(run.id, followup, "completed", {}, None, lease)
    check = next(item for item in reopened.work_items(run.id) if item.kind == "verification")
    assert check.followup_number == 1
    check = reopened.claim(run.id, check.id, lease, check_budget=True)
    reopened.reserve(run.id, check, 1000, lease)
    assert reopened.usage(run.id)["unknown_calls"] == used + 2


async def test_first_pass_uses_original_agent_fuse_beyond_twelve_requests(tmp_path):
    import json
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    source = "def ratio(x):\n    return 10 / x\n" + "".join(
        f"VALUE_{index} = {index}\n" for index in range(30))
    store, run, lease, provider, sessions = setup_source_run(tmp_path,
        head_source=source, base_source=source.replace("10 / x", "10 / (x or 1)"))
    run.configuration.options.generate_overview = False
    run.configuration.options.gap_followups = 0
    run.configuration.options.limits.model_calls = 14
    freeze_phases(sessions, run)

    class InvestigatingModel(GapModel):
        requests: int = 0

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.requests += 1
            packet = json.loads(messages[1].content)
            if self.requests < 14:
                call = {"name": "read_file", "id": str(self.requests), "args": {
                    "path": "a.py", "revision": "head", "start": self.requests + 2,
                    "end": self.requests + 2}}
            else:
                assert any("Close now" in str(message.content) for message in messages)
                assert len([message for message in messages if isinstance(message, ToolMessage)]) == 13
                handles = [source["source_ref"] for material in packet["materials"] for source in material["sources"]]
                call = {"name": "UnitCompletion", "id": "done", "args": {
                    "unresolved": completion_gap(source_refs=handles[:1])}}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call],
                usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}))])

    model = InvestigatingModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.requests == 14
    assert report.units[0].status == "completed" and report.unresolved_unit_count == 1
    with sessions() as session:
        assert len(list(session.scalars(select(ReviewRequestOrm)))) == 14


def test_queued_units_get_first_request_before_other_units_continue(tmp_path):
    from tests.review.test_budget_admission import setup_reviewers

    store, run, lease, sessions = setup_reviewers(tmp_path, calls=6)
    run.configuration.options.generate_overview = False
    freeze_phases(sessions, run)
    first, second = store.work_items(run.id)
    first = store.claim(run.id, first.id, lease, check_budget=True)
    store.reserve(run.id, first, 1000, lease)
    store.reserve(run.id, first, 1000, lease)
    with pytest.raises(BudgetExceeded):
        store.reserve(run.id, first, 1000, lease)
    assert store.claim(run.id, second.id, lease, check_budget=True)


async def test_external_gap_is_delivered_without_repository_followup(tmp_path):
    import json
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    store, run, lease, provider, _ = setup_source_run(tmp_path)

    class ExternalGapModel(GapModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if str(messages[0].content).startswith("Summarize"):
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            self.reviews += 1
            packet = json.loads(messages[1].content)
            handle = packet["materials"][0]["sources"][0]["source_ref"]
            gap = completion_gap("请确认依赖 v1.2.3 的调用是否自动分页。", [handle])
            gap["resolution_scope"] = "external"
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                tool_calls=[{"name": "UnitCompletion", "id": "gap", "args": {"unresolved": gap}}]))])

    model = ExternalGapModel()
    report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert model.reviews == 1
    assert report.status == "completed" and report.unresolved_unit_count == 1
    assert not any(work.kind == "supplement" for work in report.units)
