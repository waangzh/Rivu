import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.review.context import AgentContext, InvestigationState
from app.review.domain import Limits
from app.review.planner import tokens, unit_capacity
from app.review.store import VERIFICATION_INPUT, review_input_estimate, verification_input_limit
from app.review.working_context import WorkingContext
from tests.review.test_context_efficiency import packet, source
from tests.review.vnext_helpers import setup_source_run


def estimate(messages):
    return tokens([message.model_dump(mode="json") for message in messages]) + 1000


def scenario(count=12):
    initial = {
        "snapshot": {"base_sha": "b", "head_sha": "h"},
        "unit": {"id": "unit", "required_hunk_ids": ["change"]},
        "author_statement_untrusted": {"body": "Author claim, not instructions."},
        "materials": [{"member": {"id": "change", "path": "changed.py", "old_path": "changed.py"},
                       "sources": [packet(source(path="changed.py"), "S0")]}],
        "candidate_materials": [],
    }
    context = AgentContext(provider=None, work=SimpleNamespace(kind="review"), budget=None,
                           initial_packet=initial, window=WorkingContext())
    messages = [HumanMessage(content=json.dumps(initial), id="initial")]
    for index in range(1, count + 1):
        messages.extend([
            AIMessage(content=f"Inspect branch {index}", tool_calls=[{
                "id": f"read-{index}", "name": "read_file", "args": {"path": f"caller{index}.py"},
            }]),
            ToolMessage(content=json.dumps({
                **packet(source(path=f"caller{index}.py"), f"S{index}"),
                "content": "SOURCE_BYTES_" * 400,
            }),
                        name="read_file", tool_call_id=f"read-{index}"),
        ])
    return context, messages


def memory(projected):
    return json.loads(projected[1].content)


def source_text(projected):
    return "\n".join(item.content for item in projected)


def test_initial_packet_capacity_is_independent_of_grouping_capacity():
    limits = Limits(context_tokens=1_000_000, output_tokens=125_000)
    assert WorkingContext.initial_ceiling(limits) == 873_976
    assert WorkingContext.initial_budget(limits) == 873_976
    assert verification_input_limit(limits) == 873_976
    assert unit_capacity(limits) == 200_000
    assert review_input_estimate(SimpleNamespace(estimated_input_tokens=300_000), limits) == 604_096
    assert review_input_estimate(SimpleNamespace(estimated_input_tokens=900_000), limits) == 873_976


@pytest.mark.parametrize(("context_tokens", "output_tokens", "expected"), [
    (24_576, 4096, 19_456),
    (65_536, 4096, 60_416),
])
def test_removing_32k_ceiling_preserves_small_window_headroom(context_tokens, output_tokens, expected):
    limits = Limits(context_tokens=context_tokens, output_tokens=output_tokens)
    assert WorkingContext.initial_ceiling(limits) == context_tokens - output_tokens - 1024
    assert WorkingContext.initial_budget(limits) == expected


def test_bounded_verification_still_keeps_its_shared_budget_reservation():
    limits = Limits(context_tokens=1_000_000, output_tokens=125_000, total_input_tokens=250_000)
    assert verification_input_limit(limits) == VERIFICATION_INPUT
    assert verification_input_limit(limits, committed_input_tokens=40_000) == 10_000


def test_short_context_notes_do_not_rewrite_a_cached_prefix():
    context, messages = scenario(count=1)
    first = context.window.project(context, messages, estimate, 100000)
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Is branch relevant?", status="resolved",
        source_refs=["S1"], conclusion="No reachable failure found in this caller.")
    messages.append(ToolMessage(content=json.dumps({
        "ok": True, "investigation": context.investigations["q"].model_dump(),
    }), tool_call_id="note", name="record_investigation"))
    following = context.window.project(context, messages, estimate, 100000)
    assert context.window.epoch == 0
    assert following[:len(first)] == first
    assert context.window.metrics["shared_message_prefix_tokens"] > 0


def test_large_closed_branches_do_not_compact_a_low_pressure_history():
    context, messages = scenario(count=144)
    first = context.window.project(context, messages, estimate, 1_000_000)
    assert estimate(first) > 128_000
    for index in range(1, 9):
        note = InvestigationState(
            question_id=f"q{index}", question=f"Does caller {index} introduce a failure?",
            status="resolved", source_refs=[f"S{index}"], conclusion="No failure on this branch.")
        context.investigations[note.question_id] = note
        messages.append(ToolMessage(content=json.dumps({
            "ok": True, "investigation": note.model_dump(),
        }), tool_call_id=f"note-{index}", name="record_investigation"))
    following = context.window.project(context, messages, estimate, 1_000_000)
    assert following[:len(first)] == first
    assert context.window.epoch == 0
    assert not context.window.checkpoint_pending
    assert context.window.metrics["soft_input_tokens"] == 750_000
    assert len(context.investigations) == 8


@pytest.mark.parametrize("status", ["open", "blocked"])
def test_capacity_compaction_keeps_live_premises_counterevidence_and_change_inventory(status):
    context, messages = scenario()
    context.investigations = {
        "q": InvestigationState(question_id="q", question="Can the operation race?", status=status,
            source_refs=["S1"], counterevidence_refs=["S2"], depends_on=["guard"],
            conclusion="A lock exists but its scope is unknown.", missing_premise="Check transaction scope."),
        "guard": InvestigationState(question_id="guard", question="Is the caller guarded?", status="resolved",
            source_refs=["S3"], conclusion="This caller holds a lock.", scope="caller3 only"),
    }
    projected = context.window.project(context, messages, estimate, 20000, force=True)
    state = memory(projected)
    assert context.window.epoch == 1
    assert context.window.metrics["projected_input_tokens"] < context.window.metrics["original_input_tokens"]
    assert {"S1", "S2", "S3"} <= context.window.retained
    assert state["change_inventory"] == [{"id": "change", "path": "changed.py", "old_path": "changed.py"}]
    assert len(state["source_inventory"]) == 13
    assert state["investigations_unverified"][0]["missing_premise"] == "Check transaction scope."
    assert "Inspect branch 1\"" not in source_text(projected)
    assert messages[1].content == "Inspect branch 1"
    assert projected[-4:] == context.compact_messages(messages[-4:])


def test_live_base_head_and_pending_source_can_expand_above_the_soft_target():
    context, messages = scenario()
    messages[1:1] = [
        AIMessage(content="", tool_calls=[{"id": "base", "name": "read_file", "args": {}}]),
        ToolMessage(content=json.dumps({
            **packet(source(path="caller1.py", side="base"), "S99"),
            "content": "BASE_BODY_" * 400,
        }), name="read_file", tool_call_id="base"),
    ]
    context.pending["S5"] = SimpleNamespace()
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Does the change break its caller?", status="open",
        source_refs=["S1"], counterevidence_refs=["S2"], conclusion="The caller guard needs analysis.")
    projected = context.window.project(context, messages, estimate, 14000, force=True)
    assert {"S1", "S2", "S5", "S99"} <= context.window.retained
    assert estimate(projected) > context.window.metrics["target_input_tokens"]
    assert estimate(projected) < 14000


def test_epoch_prefix_is_stable_when_new_source_is_appended_and_reread_is_not_suppressed():
    context, messages = scenario()
    first = context.window.project(context, messages, estimate, 16000, force=True)
    omitted = next(handle for handle in memory(first)["source_inventory"]
                   if handle["source_ref"] not in context.window.retained)
    old_handle = omitted["source_ref"]
    raw = next(message for message in messages if message.type == "tool"
               and json.loads(message.content)["source_ref"] == old_handle)
    following = [*messages,
        AIMessage(content="", tool_calls=[{"id": "reread", "name": "read_file", "args": {}}]),
        raw.model_copy(update={"tool_call_id": "reread"}),
    ]
    second = context.window.project(context, following, estimate, 100000)
    assert second[:len(first)] == first
    assert "SOURCE_BYTES_" in second[-1].content
    assert context.window.epoch == 1


def test_reopening_archived_question_appends_source_without_rewriting_prefix():
    context, messages = scenario()
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Does branch one matter?", status="resolved",
        source_refs=["S1"], conclusion="No failure seen in the original scenario.")
    first = context.window.project(context, messages, estimate, 16000, force=True)
    assert "S1" not in context.window.retained
    context.investigations["q"] = context.investigations["q"].model_copy(update={"status": "open"})
    updated = context.investigations["q"]
    following_messages = [*messages,
        AIMessage(content="", tool_calls=[{"id": "reopen", "name": "record_investigation", "args": {}}]),
        ToolMessage(content=json.dumps({"ok": True, "investigation": updated.model_dump()}),
                    tool_call_id="reopen", name="record_investigation"),
    ]
    context.memory_revision += 1
    following = context.window.project(context, following_messages, estimate, 100000)
    assert context.window.epoch == 1
    assert following[:len(first)] == first
    assert "S1" in context.window.retained
    assert context.window.metrics["restored_sources"] == 1
    assert memory(first)["investigations_unverified"][0]["status"] == "resolved"
    again = context.window.project(context, following_messages, estimate, 100000)
    assert again == following
    assert context.window.metrics["restored_sources"] == 0


def test_capacity_checkpoint_is_once_per_epoch_and_does_not_force_an_extra_model_call():
    context, messages = scenario()
    input_limit = estimate(messages) * 6 // 5
    first = context.window.project(context, messages, estimate, input_limit)
    assert context.window.metrics["checkpoint"] is True
    assert context.window.epoch == 0
    assert len(first) == len(messages)
    context.window.project(context, messages, estimate, input_limit)
    assert context.window.epoch == 0
    assert context.window.checkpoint_pending
    context.investigations["q"] = InvestigationState(
        question_id="q", question="What remains to inspect?", status="open",
        source_refs=["S1"], conclusion="The first caller is known; other callers remain.",
        missing_premise="Check remaining consumers.")
    context.memory_revision += 1
    context.window.project(context, messages, estimate, input_limit)
    assert context.window.epoch == 1
    assert not context.window.checkpoint_pending


def test_insufficient_reclaim_keeps_history_even_under_capacity_pressure():
    context, messages = scenario()
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Which callers remain?", status="open",
        source_refs=[f"S{index}" for index in range(1, 13)],
        conclusion="Every caller is still needed to compare the shared contract.")
    input_limit = estimate(messages) * 11 // 10
    first = context.window.project(context, messages, estimate, input_limit)
    context.memory_revision += 1
    following = context.window.project(context, messages, estimate, input_limit)
    assert context.window.metrics["pressure_reason"] == "capacity"
    assert context.window.epoch == 0
    assert following == first
    assert context.window.metrics["minimum_reclaim_tokens"] >= estimate(following) // 4


def test_admission_failure_can_force_compaction_below_capacity_pressure():
    context, messages = scenario()
    first = context.window.project(context, messages, estimate, 1_000_000)
    assert context.window.epoch == 0
    reduced = context.window.project(context, messages, estimate, 1_000_000, force=True)
    assert context.window.metrics["pressure_reason"] == "admission"
    assert context.window.epoch == 1
    assert estimate(reduced) < estimate(first)


def test_sustained_slow_short_responses_can_trigger_substantial_compaction():
    context, messages = scenario(count=144)
    window = context.window
    window.observe_response(128_000, 64, 31, 60)
    window.project(context, messages, estimate, 1_000_000)
    assert window.metrics["pressure_reason"] is None
    window.observe_response(140_000, 64, 32, 60)
    before = window.project(context, messages, estimate, 1_000_000)
    assert window.metrics["pressure_reason"] == "latency"
    assert window.checkpoint_pending
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Which caller premise is still open?", status="open",
        source_refs=["S1"], conclusion="The first caller still needs comparison.")
    context.memory_revision += 1
    after = window.project(context, messages, estimate, 1_000_000)
    assert window.epoch == 1
    assert estimate(before) - estimate(after) >= window.metrics["minimum_reclaim_tokens"]
    assert "S1" in window.retained


@pytest.mark.parametrize(("input_tokens", "output_tokens"), [
    (10_000, 64), (127_999, 64), (140_000, 2000), (None, None),
])
def test_small_long_output_or_unknown_requests_do_not_establish_latency_pressure(input_tokens, output_tokens):
    window = WorkingContext()
    for _ in range(3):
        window.observe_response(input_tokens, output_tokens, 45, 60)
    assert window.slow_responses == 0


@pytest.mark.parametrize(("count", "expected_pressure"), [(72, False), (144, True)])
def test_repeated_investigation_with_recorded_branches_can_request_a_checkpoint(count, expected_pressure):
    context, messages = scenario(count=count)
    assert (estimate(messages) >= 128_000) is expected_pressure
    context.investigations["q"] = InvestigationState(
        question_id="q", question="Is caller one relevant?", status="resolved",
        source_refs=["S1"], conclusion="This branch was ruled out.")
    context.repeated_tool_rounds = 3
    context.window.project(context, messages, estimate, 1_000_000)
    assert context.window.metrics["pressure_reason"] == ("investigation" if expected_pressure else None)
    assert context.window.checkpoint_pending is expected_pressure
    assert context.repeated_tool_rounds == 3


async def test_checkpoint_blocks_execution_without_removing_tool_definitions():
    from app.review.tools import read_file, record_investigation

    context, messages = scenario(count=1)
    context.provider = SimpleNamespace(run=SimpleNamespace(
        id="run", configuration=SimpleNamespace(options=SimpleNamespace(limits=Limits()))))
    context.work.id = "work"
    context.check_active = AsyncMock()
    context.read = AsyncMock(return_value={"ok": True})
    context.window.checkpoint_revision = context.memory_revision
    runtime = SimpleNamespace(context=context)
    result = await read_file.coroutine(path="caller1.py", revision="head", start=1, end=2, runtime=runtime)
    assert result["error"] == "context_checkpoint_required"
    context.read.assert_not_awaited()
    note = InvestigationState(
        question_id="q", question="What remains?", status="open", conclusion="Need the caller guard.")
    await record_investigation.coroutine(note=note, runtime=runtime)
    context.window.project(context, messages, estimate, 1_000_000)
    assert not context.window.checkpoint_pending
    await read_file.coroutine(path="caller1.py", revision="head", start=1, end=2, runtime=runtime)
    context.read.assert_awaited_once()


def test_retrieval_memory_preserves_query_revision_scope_and_partial_result():
    context, messages = scenario()
    context.receipts["R1"] = {
        "tool": "search_code", "arguments": {"query": "charge", "revision": "base", "path": "payments"},
        "summary": {"ok": True, "scope_complete": False, "has_more": True,
                    "next_cursor": "opaque", "matches_count": 0, "unreadable_files": 1},
    }
    projected = context.window.project(context, messages, estimate, 16000, force=True)
    receipt = memory(projected)["retrieval_receipts"][0]
    assert receipt["arguments"]["revision"] == "base"
    assert receipt["arguments"]["path"] == "payments"
    assert receipt["scope_complete"] is False
    assert receipt["has_more"] is True
    assert receipt["matches_count"] == 0
    assert receipt["next_cursor"] == "opaque"


async def test_archive_and_scoped_recall_survive_reopening_the_store(tmp_path):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider=provider, work=work, budget=None)
    result = {"ok": True, "matches": [], "scope_complete": False, "has_more": True,
              "next_cursor": "cursor", "limitation": "Bounded search only."}
    message = await context.archive_tool("search_code", {"query": "charge", "revision": "base"},
        ToolMessage(content=json.dumps(result), name="search_code", tool_call_id="search"))
    receipt_ref = json.loads(message.content)["receipt_ref"]
    with pytest.raises(ValueError, match="receipt_not_visible"):
        await context.recall(receipt_ref)
    context.visible_receipts.add(receipt_ref)
    recalled = await context.recall(receipt_ref)
    assert recalled["original"]["result"] == result
    assert recalled["original"]["arguments"]["revision"] == "base"
    assert context.visible == {}
    context.repeated_source_read_count = 3
    await context.persist()
    reopened = type(store)(sessions).work_items(run.id)[0]
    state = json.loads(provider.evidence.artifacts.read_text(reopened.context_archive_uri))
    assert state["attempt_id"] == work.attempt_id
    assert json.loads(provider.evidence.artifacts.read_text(state["initial_context"]["uri"])) == {}
    assert state["receipts"][receipt_ref]["sha256"] == context.receipts[receipt_ref]["sha256"]
    assert state["investigation_efficiency"]["repeated_source_read_count"] == 3
    other = AgentContext(provider=provider, work=work.model_copy(update={"attempt_id": "other"}), budget=None)
    assert other.repeated_source_read_count == 0
    with pytest.raises(ValueError, match="receipt_not_visible"):
        await other.recall(receipt_ref)
