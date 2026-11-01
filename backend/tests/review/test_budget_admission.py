import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm, ReviewWorkItemOrm
from app.review.domain import Overview, WorkItem
from app.review.orchestrator import ReviewOrchestrator
from app.review.store import OVERVIEW_INPUT, VERIFICATION_INPUT, BudgetExceeded
from tests.review.vnext_helpers import setup_run, submit_candidate


def setup_reviewers(tmp_path, *, calls=3, input_tokens=100000):
    store, run, lease, sessions = setup_run(tmp_path, calls=calls, input_tokens=input_tokens)
    run.plan.units.append(run.plan.units[0].model_copy(update={
        "id": "u2", "paths": ["b.py"], "required_hunk_ids": ["h2"]}))
    work = WorkItem(id=uuid4().hex, kind="review", unit_id="u2")
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
        session.add(ReviewWorkItemOrm(id=work.id, run_id=run.id, kind="review",
            subject=work.unit_id, payload=work.model_dump(mode="json")))
    return store, run, lease, sessions


def complete_reviewer(store, run, lease, work, *, candidate=True):
    if candidate:
        hunk = next(unit.required_hunk_ids[0] for unit in run.plan.units if unit.id == work.unit_id)
        submit_candidate(store, run, lease, work, hunk)
    store.finish_work(run.id, work, "completed", {"unresolved_reason": None}, None, lease)


def test_small_call_budget_starts_only_one_reviewer_atomically(tmp_path):
    store, run, lease, _ = setup_reviewers(tmp_path)
    reviews = store.work_items(run.id)

    def claim(work):
        try:
            return store.claim(run.id, work.id, lease, check_budget=True)
        except BudgetExceeded as exc:
            assert str(exc) == "model_call_limit"
            return None

    with ThreadPoolExecutor(2) as pool:
        claimed = [work for work in pool.map(claim, reviews) if work is not None]
    assert len(claimed) == 1
    reviewer = claimed[0]
    ticket, available = store.reserve(run.id, reviewer, 1000, lease, with_remaining=True)
    assert available["model_calls"] == 1
    store.settle(run.id, ticket, None, lease)
    complete_reviewer(store, run, lease, reviewer)
    check = next(work for work in store.work_items(run.id) if work.kind == "verification")
    check = store.claim(run.id, check.id, lease, check_budget=True)
    store.reserve(run.id, check, 1000, lease)
    store.reserve(run.id, None, 1000, lease, overview=True)
    usage = store.usage(run.id)
    assert usage["model_calls"] == 3
    assert usage["committed_input_tokens"] == 3000
    assert usage["input_tokens"] is None


def test_output_budget_without_verification_space_does_not_start_reviewer(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path, calls=3)
    run.configuration.options.limits.total_output_tokens = 2 * 4096
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    work = store.work_items(run.id)[0]
    with pytest.raises(BudgetExceeded, match="output_token_limit"):
        store.claim(run.id, work.id, lease, check_budget=True)
    assert store.work_items(run.id)[0].status == "queued"
    assert store.usage(run.id)["model_calls"] == 0


def test_parallel_reviewers_keep_both_late_candidate_first_requests(tmp_path):
    store, run, lease, _ = setup_reviewers(tmp_path, calls=5)
    reviews = store.work_items(run.id)
    with ThreadPoolExecutor(2) as pool:
        reviewers = list(pool.map(
            lambda work: store.claim(run.id, work.id, lease, check_budget=True), reviews))
    with ThreadPoolExecutor(2) as pool:
        tickets = list(pool.map(
            lambda work: store.reserve(run.id, work, 1000, lease), reviewers))
    assert len(tickets) == 2
    # Neither candidate exists until both reviewer requests have been admitted.
    for reviewer in reviewers:
        complete_reviewer(store, run, lease, reviewer)
    checks = [work for work in store.work_items(run.id) if work.kind == "verification"]
    assert len(checks) == 2
    for check in checks:
        check = store.claim(run.id, check.id, lease, check_budget=True)
        store.reserve(run.id, check, VERIFICATION_INPUT, lease)
    store.reserve(run.id, None, OVERVIEW_INPUT, lease, overview=True)
    assert store.usage(run.id)["model_calls"] == 5


def test_claimed_reviewer_first_request_is_protected_before_it_reserves(tmp_path):
    store, run, lease, _ = setup_reviewers(tmp_path, calls=5)
    reviewers = [store.claim(run.id, work.id, lease, check_budget=True)
                 for work in store.work_items(run.id)]
    store.reserve(run.id, reviewers[0], 1000, lease)
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        store.reserve(run.id, reviewers[0], 1000, lease)
    store.reserve(run.id, reviewers[1], 1000, lease)


def test_candidate_transfers_buffer_without_double_counting(tmp_path):
    store, run, lease, _ = setup_run(tmp_path, calls=4)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease, check_budget=True)
    store.reserve(run.id, work, 1000, lease)
    submit_candidate(store, run, lease, work)
    # The reviewer can finish while the candidate's first request stays protected.
    store.reserve(run.id, work, 1000, lease)
    complete_reviewer(store, run, lease, work, candidate=False)
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    check = store.claim(run.id, check.id, lease, check_budget=True)
    store.reserve(run.id, check, 1000, lease)
    store.reserve(run.id, None, 1000, lease, overview=True)


def test_verifier_continuation_preserves_other_inflight_reviewer_buffer(tmp_path):
    store, run, lease, _ = setup_reviewers(tmp_path, calls=5)
    reviewers = [store.claim(run.id, work.id, lease, check_budget=True)
                 for work in store.work_items(run.id)]
    for reviewer in reviewers:
        store.reserve(run.id, reviewer, 1000, lease)
    complete_reviewer(store, run, lease, reviewers[0])
    first = next(work for work in store.work_items(run.id) if work.kind == "verification")
    first = store.claim(run.id, first.id, lease, check_budget=True)
    store.reserve(run.id, first, 1000, lease)
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        store.reserve(run.id, first, 1000, lease)
    complete_reviewer(store, run, lease, reviewers[1])
    second = next(work for work in store.work_items(run.id)
                  if work.kind == "verification" and work.id != first.id)
    second = store.claim(run.id, second.id, lease, check_budget=True)
    store.reserve(run.id, second, 1000, lease)
    store.reserve(run.id, None, 1000, lease, overview=True)


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
def test_reviewer_without_candidate_releases_buffer_on_exit(tmp_path, status):
    store, run, lease, _ = setup_reviewers(tmp_path, calls=4)
    first, second = store.work_items(run.id)
    first = store.claim(run.id, first.id, lease, check_budget=True)
    store.reserve(run.id, first, 1000, lease)
    assert store.can_start(run.id, second) == "model_call_limit"
    store.finish_work(run.id, first, status, None, None, lease)
    assert store.can_start(run.id, second) is None
    assert store.claim(run.id, second.id, lease, check_budget=True) is not None


def test_recovery_keeps_unknown_cost_and_rebuilds_attempt_buffer(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path, calls=4)
    first = store.claim(run.id, store.work_items(run.id)[0].id, lease, check_budget=True)
    ticket = store.reserve(run.id, first, 1000, lease)
    store.settle(run.id, ticket, None, lease)
    submit_candidate(store, run, lease, first)
    reopened = type(store)(sessions)
    reopened.begin(run.id, lease)
    old_check = next(work for work in reopened.work_items(run.id) if work.kind == "verification")
    old_check = reopened.claim(run.id, old_check.id, lease, check_budget=True)
    reopened.reserve(run.id, old_check, 1000, lease)
    reopened.finish_work(run.id, old_check, "completed", {}, None, lease)
    reviewer = next(work for work in reopened.work_items(run.id) if work.kind == "review")
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        reopened.claim(run.id, reviewer.id, lease, check_budget=True)
    assert reopened.usage(run.id)["committed_input_tokens"] == 2000
    assert reopened.usage(run.id)["unknown_calls"] == 2


@pytest.mark.parametrize("resource", ["calls", "input", "output"])
def test_verifier_continuation_leaves_other_candidate_first_request(tmp_path, resource):
    store, run, lease, sessions = setup_run(tmp_path, calls=10)
    limits = run.configuration.options.limits
    if resource == "calls":
        limits.total_model_calls = 4
    elif resource == "input":
        limits.total_input_tokens = 2 * VERIFICATION_INPUT + OVERVIEW_INPUT + 1000
    else:
        limits.total_output_tokens = 4 * limits.output_tokens
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease, check_budget=True)
    ticket = store.reserve(run.id, reviewer, 1000, lease)
    store.settle(run.id, ticket, {"input_tokens": 1000, "output_tokens": limits.output_tokens}, lease)
    candidate = submit_candidate(store, run, lease, reviewer)
    store.submit(run.id, reviewer, candidate.model_copy(update={
        "id": uuid4().hex, "tool_call_id": "second"}), lease)
    complete_reviewer(store, run, lease, reviewer, candidate=False)
    checks = [work for work in store.work_items(run.id) if work.kind == "verification"]
    first = store.claim(run.id, checks[0].id, lease, check_budget=True)
    store.reserve(run.id, first, VERIFICATION_INPUT, lease)
    with pytest.raises(BudgetExceeded):
        store.reserve(run.id, first, 1000, lease)
    second = store.claim(run.id, checks[1].id, lease, check_budget=True)
    store.reserve(run.id, second, VERIFICATION_INPUT, lease)
    store.reserve(run.id, None, OVERVIEW_INPUT, lease, overview=True)


def test_candidate_burst_still_gets_some_verification_without_free_calls(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path, calls=3)
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease, check_budget=True)
    store.reserve(run.id, reviewer, 1000, lease)
    candidate = submit_candidate(store, run, lease, reviewer)
    store.submit(run.id, reviewer, candidate.model_copy(update={
        "id": uuid4().hex, "tool_call_id": "second"}), lease)
    complete_reviewer(store, run, lease, reviewer, candidate=False)
    checks = [work for work in store.work_items(run.id) if work.kind == "verification"]
    first = store.claim(run.id, checks[0].id, lease, check_budget=True)
    store.reserve(run.id, first, 1000, lease)
    assert store.can_start(run.id, checks[1]) == "model_call_limit"
    assert len(store.candidates(run.id)) == 2
    with sessions() as session:
        assert len(list(session.scalars(select(ReviewRequestOrm)))) == 2


def test_unbounded_calls_do_not_add_speculative_reviewer_protection(tmp_path):
    store, run, lease, sessions = setup_reviewers(tmp_path)
    run.configuration.options.limits.total_model_calls = None
    run.configuration.options.limits.total_input_tokens = None
    run.configuration.options.limits.total_output_tokens = None
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    for work in store.work_items(run.id):
        work = store.claim(run.id, work.id, lease, check_budget=True)
        store.reserve(run.id, work, 1000, lease)


async def test_scheduler_waits_then_starts_next_reviewer_after_buffer_release(tmp_path, monkeypatch):
    store, run, lease, _ = setup_reviewers(tmp_path, calls=4)
    started = []
    blocked = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_claim = store.claim

    def claim(*args, **kwargs):
        try:
            return original_claim(*args, **kwargs)
        except BudgetExceeded:
            loop.call_soon_threadsafe(blocked.set)
            raise

    monkeypatch.setattr(store, "claim", claim)

    class Runner:
        model = None

        async def run(self, work, provider, budget):
            started.append(work.unit_id)
            await budget.reserve(work, 1000)
            if work.unit_id == "u1":
                await asyncio.wait_for(blocked.wait(), timeout=5)
            await asyncio.to_thread(store.finish_work, run.id, work,
                "completed", {"unresolved_reason": None}, None, lease)

    async def overview(report, provider, budget, model):
        await budget.reserve(None, 1000, overview=True)
        return Overview(status="failed", error_code="fixture_overview")

    monkeypatch.setattr("app.review.orchestrator.generate_overview", overview)
    report = await ReviewOrchestrator(store, Runner()).run(
        run.id, SimpleNamespace(run=run), lease)
    assert blocked.is_set()
    assert started == ["u1", "u2"]
    assert all(work.status == "completed" for work in report.units)
    assert report.usage["model_calls"] == 3
