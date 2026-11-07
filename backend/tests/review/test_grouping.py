import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import delete, select, update

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm, ReviewWorkItemOrm, WorkerJobOrm
from app.models.review.diff import ChangedFile
from app.review.context import AgentContext
from app.review.domain import utcnow
from app.review.configuration import grouping_min_files
from app.review.grouping import (
    FileGroup, GroupingOutput, freeze_semantic_groups, grouped_plan, grouping_requested,
)
from app.review.planner import plan_review
from app.review.store import OVERVIEW_INPUT, VERIFICATION_INPUT, BudgetExceeded, LeaseLost, ReviewStopped
from tests.review.vnext_helpers import setup_run
from tests.review.test_planner import source_change


def changes(*paths):
    return [ChangedFile(file_path=path, change_type="modified", additions=1, deletions=0)
            for path in paths]


def test_grouping_output_assigns_each_file_once_and_preserves_label(tmp_path):
    store, run, lease, _ = setup_run(tmp_path)
    run.changed_files = changes("auth/controller.py", "auth/service.py", "auth/test.py", "orders.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"
    output = GroupingOutput(groups=[
        FileGroup(label="Authentication", files=[2, 0, 0, 99]),
    ])

    plan = grouped_plan(run, output)

    assert sorted(path for unit in plan.units for path in unit.paths) == sorted(
        change.file_path for change in run.changed_files)
    auth = next(unit for unit in plan.units if "auth/controller.py" in unit.paths)
    assert auth.label == "Authentication"
    assert plan.grouping_status == "model"


def test_grouping_failure_reuses_deterministic_relation_plan(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = changes("core/engine.ts", "server/parser.ts",
                                "tests/engine.test.ts", "unrelated.ts")
    relations = [(0, "core/engine.ts", "server/parser.ts"),
                 (1, "core/engine.ts", "tests/engine.test.ts")]
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options, relations)
    run.plan.grouping_status = "pending"

    plan = grouped_plan(run, reason="grouping_unavailable")

    assert plan.grouping_status == "fallback"
    assert plan.grouping_reason == "grouping_unavailable"
    assert any(unit.paths == ["core/engine.ts", "server/parser.ts", "tests/engine.test.ts"]
               for unit in plan.units)
    assert any(unit.paths == ["unrelated.ts"] for unit in plan.units)
    assert plan.units == run.plan.units
    assert run.plan.grouping_status == "pending"


def test_grouping_failure_keeps_small_change_bundle(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = changes("a.py", "b.py", "c.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"

    plan = grouped_plan(run, reason="grouping_unavailable")

    assert [unit.paths for unit in plan.units] == [["a.py", "b.py", "c.py"]]
    assert plan.grouping_status == "fallback"
    assert plan.grouping_reason == "grouping_unavailable"


def test_grouping_requested_follows_depth_policy_and_reuse(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = changes("a.py", "b.py", "c.py", "d.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    assert grouping_requested(run, grouping_min_files("quick")) is False
    assert grouping_requested(run, grouping_min_files("standard")) is True
    assert grouping_requested(run, grouping_min_files("deep")) is True

    run.changed_files = changes("a.py", "b.py", "c.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    assert grouping_requested(run, grouping_min_files("standard")) is False
    assert grouping_requested(run, grouping_min_files("deep")) is True

    run.plan.units[0].reused_from = "previous:work"
    assert grouping_requested(run, grouping_min_files("deep")) is False


@pytest.mark.parametrize("model_grouping", [True, False])
def test_api_risk_survives_persisted_run_and_grouping(tmp_path, model_grouping):
    from app.review.domain import Run

    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = source_change("src/index.ts", 'export { fetch } from "./v1";\n',
                                      'export { fetch } from "./v2";\n')
    run.changed_files += changes("a.py", "b.py", "c.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    restored = Run.model_validate_json(run.model_dump_json())
    output = GroupingOutput(groups=[FileGroup(label="API", files=[3])]) if model_grouping else None

    rebuilt = grouped_plan(restored, output, reason=None if model_grouping else "grouping_unavailable")

    assert rebuilt.units[0].paths == ["src/index.ts"]
    assert rebuilt.units[0].risk == "high"
    assert all(unit.risk == "low" for unit in rebuilt.units[1:])


def test_model_grouping_cannot_split_strong_relations_when_capacity_allows(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = changes("core/engine.ts", "server/parser.ts",
                                "tests/engine.test.ts", "unrelated.ts")
    relations = [(0, "core/engine.ts", "server/parser.ts"),
                 (1, "core/engine.ts", "tests/engine.test.ts")]
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options, relations)
    output = GroupingOutput(groups=[FileGroup(label=str(index), files=[index])
                                    for index in range(4)])
    plan = grouped_plan(run, output)
    assert any(unit.paths == ["core/engine.ts", "server/parser.ts", "tests/engine.test.ts"]
               for unit in plan.units)
    assert any(unit.paths == ["unrelated.ts"] for unit in plan.units)
    assert sorted(path for unit in plan.units for path in unit.paths) == sorted(
        change.file_path for change in run.changed_files)
    fallback = grouped_plan(run, reason="grouping_unavailable")
    assert fallback.grouping_status == "fallback"
    assert any(unit.paths == ["core/engine.ts", "server/parser.ts", "tests/engine.test.ts"]
               for unit in fallback.units)
    assert any(unit.paths == ["unrelated.ts"] for unit in fallback.units)


def test_strong_relations_take_priority_over_large_model_group(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    paths = [f"pkg/file{index:02d}.py" for index in range(12)]
    run.changed_files = changes(*paths)
    relations = [(0, paths[0], paths[-1])]
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options, relations)
    output = GroupingOutput(groups=[FileGroup(label="all", files=list(range(12)))])
    plan = grouped_plan(run, output)
    assert all(len(unit.paths) <= 10 for unit in plan.units)
    assert any({paths[0], paths[-1]} <= set(unit.paths) for unit in plan.units)
    assert len({path for unit in plan.units for path in unit.paths}) == 12


def test_repeated_immutable_tool_result_is_counted_for_advisory_warning():
    context = AgentContext(provider=None, work=None, budget=None)
    arguments = {"path": "a.py", "revision": "head", "start": 1, "end": 2}
    result = {"ok": True, "source": {"commit_sha": "head-sha", "side": "head", "path": "a.py",
                                   "start_line": 1, "end_line": 2},
              "content": "VALUE = 1\n", "source_ref": "S1"}

    assert context.note_tool_result("read_file", arguments, result) == 1
    assert context.note_tool_result("read_file", arguments, result) == 2
    assert context.note_tool_result("read_file", arguments, result) == 3


def test_duplicate_source_bodies_are_compacted_but_handles_are_kept():
    source = {
        "commit_sha": "a" * 40, "side": "head", "path": "a.py",
        "review_id": "review", "content_hash": "b" * 64,
        "start_line": 1, "end_line": 2, "id": "c" * 64,
    }
    messages = [
        HumanMessage(content=json.dumps({
            "materials": [{"sources": [{"source": source, "content": "VALUE = 1\n"}]}],
        })),
        ToolMessage(content=json.dumps({
            "source": source, "source_ref": "S1", "content": "VALUE = 1\n",
        }), tool_call_id="call-1"),
    ]
    compacted = AgentContext.compact_messages(messages)
    payload = json.loads(compacted[1].content)
    assert payload["source_ref"] == "S1"
    assert payload["content_omitted"] is True
    assert "VALUE = 1" not in payload["content"]


def pending_run(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    run.changed_files = changes("a.py", "b.py", "c.py", "d.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"
    with sessions.begin() as session:
        session.execute(delete(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run.id))
        row = session.get(ReviewTaskOrm, run.id)
        row.task_snapshot = run.model_dump(mode="json")
    return store, run, lease, sessions


class GroupModel:
    def __init__(self, *, invalid=False, unknown=False):
        self.calls = []
        self.invalid, self.unknown = invalid, unknown

    def bind_tools(self, tools, **kwargs):
        assert tools == [GroupingOutput]
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content="", usage_metadata=(None if self.unknown else {
            "input_tokens": 80, "output_tokens": 20, "total_tokens": 100}), tool_calls=[{
                "id": "groups", "name": "GroupingOutput",
                "args": {"groups": "invalid"} if self.invalid else {
                    "groups": [{"label": "Feature", "files": [0, 3]},
                               {"label": "Support", "files": [1, 2]}]},
            }])


async def test_grouping_is_frozen_before_dispatch_and_not_replayed(tmp_path):
    store, run, lease, sessions = pending_run(tmp_path)
    model = GroupModel()
    assert store.work_items(run.id) == []
    frozen = await freeze_semantic_groups(run, store, lease, model)
    assert frozen.plan.grouping_status == "model"
    assert len(store.work_items(run.id)) == 2
    assert any(unit.paths == ["a.py", "d.py"] for unit in frozen.plan.units)
    again = await freeze_semantic_groups(store.get(run.id), store, lease, model)
    assert again.plan == frozen.plan
    assert len(model.calls) == 1
    assert len(store.work_items(run.id)) == 2
    packet = json.loads(model.calls[0][1].content)
    assert set(packet["files"][0]) == {"index", "path", "status", "additions", "deletions"}
    assert store.usage(run.id)["input_tokens"] == 80
    with sessions() as session:
        ticket = session.scalar(select(ReviewRequestOrm))
        assert ticket.kind == "grouping" and ticket.state == "settled"


@pytest.mark.parametrize(("resource", "value", "reason"), [
    ("total_model_calls", 3, "model_call_limit"),
    ("total_input_tokens", OVERVIEW_INPUT + VERIFICATION_INPUT, "input_token_limit"),
    ("total_output_tokens", 3 * 4096, "output_token_limit"),
])
async def test_grouping_skips_before_spending_minimum_review_budget(tmp_path, resource, value, reason):
    store, run, lease, sessions = pending_run(tmp_path)
    setattr(run.configuration.options.limits, resource, value)
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    model = GroupModel()
    frozen = await freeze_semantic_groups(run, store, lease, model)
    assert model.calls == []
    assert frozen.plan.grouping_status == "fallback"
    assert frozen.plan.grouping_reason == f"grouping_budget:{reason}"
    assert store.usage(run.id)["model_calls"] == 0
    assert len(store.work_items(run.id)) == 4
    first = store.work_items(run.id)[0]
    assert store.claim(run.id, first.id, lease, check_budget=True) is not None


async def test_grouping_keeps_one_review_verification_and_overview_with_four_calls(tmp_path):
    store, run, lease, sessions = pending_run(tmp_path)
    run.configuration.options.limits.total_model_calls = 4
    run.configuration.options.limits.model_calls = 1
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    model = GroupModel()
    frozen = await freeze_semantic_groups(run, store, lease, model)
    assert len(model.calls) == 1
    assert frozen.plan.grouping_status == "model"
    work = store.work_items(run.id)[0]
    work = store.claim(run.id, work.id, lease, check_budget=True)
    _, available = store.reserve(run.id, work, 1000, lease, with_remaining=True)
    assert available["agent_model_calls"] == 1
    assert available["model_calls"] == 1


async def test_unknown_grouping_request_is_not_recharged_after_recovery(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    store.reserve_grouping(run.id, 1234, lease)
    reopened = type(store)(store.sessions)
    model = GroupModel()
    frozen = await freeze_semantic_groups(reopened.get(run.id), reopened, lease, model)
    assert frozen.plan.grouping_status == "fallback"
    assert frozen.plan.grouping_reason == "previous_grouping_request_not_replayed"
    assert model.calls == []
    assert reopened.usage(run.id)["unknown_calls"] == 1
    assert reopened.usage(run.id)["committed_input_tokens"] == 1234
    assert len(reopened.work_items(run.id)) == 4


@pytest.mark.parametrize("unknown", [False, True])
async def test_invalid_grouping_keeps_usage_and_falls_back(tmp_path, unknown):
    store, run, lease, _ = pending_run(tmp_path)
    frozen = await freeze_semantic_groups(run, store, lease, GroupModel(invalid=True, unknown=unknown))
    assert frozen.plan.grouping_status == "fallback"
    assert len(store.work_items(run.id)) == 4
    assert store.usage(run.id)["unknown_calls"] == int(unknown)
    assert store.usage(run.id)["model_calls"] == 1
    assert store.usage(run.id)["committed_input_tokens"] > 0


def test_concurrent_grouping_reservation_is_single_and_freeze_is_idempotent(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        tickets = list(pool.map(lambda _: store.reserve_grouping(run.id, 1000, lease), range(2)))
    assert sum(ticket is not None for ticket in tickets) == 1
    plan = grouped_plan(run)
    with ThreadPoolExecutor(2) as pool:
        plans = list(pool.map(lambda _: store.freeze_grouping(run.id, plan, lease), range(2)))
    assert plans[0].plan == plans[1].plan
    assert len(store.work_items(run.id)) == 4


@pytest.mark.parametrize("fault", ["missing", "path", "reused", "duplicate"])
def test_grouping_freeze_refuses_invalid_scope_atomically(tmp_path, fault):
    store, run, lease, _ = pending_run(tmp_path)
    plan = grouped_plan(run)
    if fault == "missing":
        plan.units.pop()
    elif fault == "path":
        plan.units[0].paths = ["untrusted.py"]
    elif fault == "reused":
        plan.units[0].reused_from = "untrusted:work"
    else:
        plan.units.append(plan.units[0])
    with pytest.raises(ValueError, match="grouping_"):
        store.freeze_grouping(run.id, plan, lease)
    assert store.get(run.id).plan.grouping_status == "pending"
    assert store.work_items(run.id) == []


@pytest.mark.parametrize("stop", ["cancel", "lease", "deadline"])
def test_grouping_freeze_rechecks_lifecycle_fence(tmp_path, stop):
    store, run, lease, sessions = pending_run(tmp_path)
    plan = grouped_plan(run)
    if stop == "cancel":
        store.cancel(run.id)
        expected = ReviewStopped
    elif stop == "lease":
        with sessions.begin() as session:
            session.execute(update(WorkerJobOrm).values(lease_until=utcnow() - timedelta(seconds=1)))
        expected = LeaseLost
    else:
        run.hard_deadline = utcnow() - timedelta(seconds=1)
        with sessions.begin() as session:
            session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
        expected = BudgetExceeded
    with pytest.raises(expected):
        store.freeze_grouping(run.id, plan, lease)
    assert store.work_items(run.id) == []


async def test_cancelled_grouping_stays_unknown_and_does_not_dispatch(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    entered = asyncio.Event()

    class WaitingModel(GroupModel):
        async def ainvoke(self, messages):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(freeze_semantic_groups(run, store, lease, WaitingModel()))
    await entered.wait()
    store.cancel(run.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.usage(run.id)["unknown_calls"] == 1
    assert store.work_items(run.id) == []


def test_tool_loop_requires_consecutive_identical_calls_and_results():
    context = AgentContext(provider=None, work=None, budget=None)
    request, response = {"path": "a.py"}, {"content": "same"}
    for round_number in range(1, 6):
        context.note_tool_result("read_file", request, response)
        assert context.begin_round() == round_number
    context.note_tool_result("read_file", {"path": "b.py"}, response)
    assert context.begin_round() == 1
    context.note_tool_result("read_file", request, response)
    assert context.begin_round() == 1
    context.note_tool_result("read_file", request, {"content": "changed"})
    assert context.begin_round() == 1
    assert context.begin_round() == 0


def test_parallel_tool_order_does_not_reset_a_loop_but_new_candidate_does():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(5):
        paths = ["a.py", "b.py"] if index % 2 else ["b.py", "a.py"]
        for path in paths:
            context.note_tool_result("read_file", {"path": path}, {"content": "same"})
        assert context.begin_round() == index + 1
    context.note_tool_result("read_file", {"path": "a.py"}, {"content": "same"})
    context.note_tool_result("read_file", {"path": "b.py"}, {"content": "same"})
    context.note_tool_result("submit_candidate", {}, {"ok": True, "candidate_id": "new"})
    assert context.begin_round() == 1


def test_pagination_and_base_head_comparison_are_distinct_requests():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(5):
        cursor = str(index)
        context.cursors[cursor] = {"scope": "search", "position": index, "column": 0}
        context.note_tool_result("search_code", {"cursor": cursor}, {"matches": [], "next_cursor": str(index + 1)})
        assert context.begin_round() == 1
    for side in ("base", "head"):
        context.note_tool_result("read_file", {"path": "a.py", "revision": side}, {"content": "same"})
        assert context.begin_round() == 1


def test_opaque_cursor_changes_do_not_reset_a_tool_loop():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(4):
        cursor = str(index)
        context.cursors[cursor] = {"scope": "search", "position": 0, "column": 0}
        context.note_tool_result("search_code", {"cursor": cursor},
                                 {"matches": [], "cursor": cursor, "next_cursor": str(index + 1)})
        assert context.begin_round() == index + 1


def test_overlapping_source_requests_do_not_establish_a_tool_loop():
    context = AgentContext(provider=None, work=None, budget=None)

    def read(start, end, side="head", tool="read_file"):
        source = {"commit_sha": side + "-sha", "side": side, "path": "a.py",
                  "start_line": start, "end_line": end}
        context.note_tool_result(tool, {"start": start, "end": end, "revision": side},
                                 {"ok": True, "source": source, "content": "fixed bytes"})
        return context.begin_round()

    assert read(1, 100) == 1
    assert read(101, 200) == 1
    assert read(20, 180) == 1
    assert read(30, 170, tool="read_symbol") == 1
    assert read(40, 160) == 1
    assert read(150, 220) == 1
    assert read(40, 160, side="base") == 1


def test_pending_grouping_cannot_be_reported_as_completed(tmp_path):
    from app.review.report import build_report

    store, run, _, _ = pending_run(tmp_path)
    report = build_report(run, [], [], store.usage(run.id))
    assert report.status == "partial"
    assert "grouping_incomplete" in report.reasons
    assert "scope_empty" not in report.reasons
