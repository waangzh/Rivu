import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.models.review.diff import ChangedFile
from app.review.context import AgentContext, InvestigationState
from app.review.domain import Plan, SourceRef, Unit


def source(path="a.py", start=1, end=2, side="head"):
    return SourceRef(
        id="a" * 64, review_id="review", commit_sha=("b" if side == "head" else "c") * 40,
        side=side, path=path, start_line=start, end_line=end, content_hash="d" * 64,
    ).model_dump()


def packet(ref, handle="S1"):
    return {"ok": True, "source": ref, "source_ref": handle,
            "content": "SOURCE_BYTES_" * 100}


def test_compaction_is_append_only_and_keeps_the_first_source_body():
    initial = HumanMessage(content=json.dumps({"materials": [
        {"sources": [packet(source(start=1, end=100))]},
    ]}))
    messages = [
        initial,
        ToolMessage(content=json.dumps(packet(source(start=101, end=200), "S2")),
                    tool_call_id="second"),
        ToolMessage(content=json.dumps(packet(source(start=20, end=180), "S3")),
                    tool_call_id="overlap"),
    ]
    first_projection = AgentContext.compact_messages(messages)
    assert first_projection[0].content == initial.content
    assert "SOURCE_BYTES_" in first_projection[1].content
    overlap = json.loads(first_projection[2].content)
    assert overlap["content_omitted"] is True
    assert overlap["source_ref"] == "S3"
    assert overlap["source"]["start_line"] == 20
    assert "SOURCE_BYTES_" not in overlap["content"]
    assert "SOURCE_BYTES_" in messages[2].content
    following = [*messages, ToolMessage(
        content=json.dumps(packet(source(start=20, end=180, side="base"), "S4")),
        tool_call_id="base")]
    second_projection = AgentContext.compact_messages(following)
    assert [item.content for item in second_projection[:3]] == [
        item.content for item in first_projection]
    assert "SOURCE_BYTES_" in second_projection[-1].content


def test_model_prose_cannot_suppress_source_and_partial_overlap_keeps_new_bytes():
    model = AIMessage(content=json.dumps(packet(source(start=1, end=100))))
    official = ToolMessage(content=json.dumps(packet(source(start=1, end=100))),
                           tool_call_id="official")
    extending = ToolMessage(content=json.dumps(packet(source(start=90, end=110), "S2")),
                            tool_call_id="extending")
    projected = AgentContext.compact_messages([model, official, extending])
    assert projected[0].content == model.content
    assert "SOURCE_BYTES_" in projected[1].content
    assert "SOURCE_BYTES_" in projected[2].content


def test_repeated_search_results_keep_scope_and_cursor_control_fields():
    result = {"ok": True, "source": "search_hint", "pagination_id": "scope",
              "has_more": False, "next_cursor": None, "scope_complete": True,
              "matches": [{"path": "a.py", "line": 1, "text": "HINT_TEXT"}]}
    messages = [ToolMessage(content=json.dumps(result), tool_call_id=str(index),
                            name="search_code") for index in range(2)]
    projected = AgentContext.compact_messages(messages)
    assert "HINT_TEXT" in projected[0].content
    duplicate = json.loads(projected[1].content)
    assert duplicate["same_result_as_tool_call"] == "0"
    assert duplicate["scope_complete"] is True
    assert duplicate["next_cursor"] is None
    assert "matches" not in duplicate


def test_memory_only_round_does_not_reset_repeated_investigation_loop():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(3):
        context.note_tool_result("search_code", {"query": "race"}, {"ok": True, "matches": []})
        assert context.begin_round() == index + 1
    context.note_tool_result("record_investigation", {}, {"ok": True})
    assert context.begin_round() == 3
    context.note_tool_result("search_code", {"query": "race"}, {"ok": True, "matches": []})
    assert context.begin_round() == 4


def test_alternating_identical_tool_cycles_are_detected_without_source_novelty_heuristic():
    context = AgentContext(provider=None, work=None, budget=None)
    for cycle in range(1, 6):
        for query in ("caller", "guard"):
            context.note_tool_result("search_code", {"query": query},
                                     {"ok": True, "matches": []})
            repeated = context.begin_round()
        assert repeated == cycle
    assert context.repeated_cycle_length == 2
    assert context.repeated_request_count == 5
    context.note_tool_result("read_file", {"start": 2, "end": 2},
                             {"ok": True, "content": "same already received source"})
    assert context.begin_round() == 1


def test_persisted_candidate_and_new_search_result_break_a_repeated_cycle():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(3):
        for query in ("caller", "guard"):
            context.note_tool_result("search_code", {"query": query}, {"ok": True, "matches": []})
            context.begin_round()
    context.note_tool_result("submit_candidate", {}, {"ok": True, "candidate_id": "new"})
    assert context.begin_round() == 1
    context.note_tool_result("search_code", {"query": "caller"},
                             {"ok": True, "matches": [{"path": "new.py", "line": 1}]})
    assert context.begin_round() == 1


def test_contained_reads_with_different_ranges_raise_only_an_efficiency_signal():
    context = AgentContext(provider=None, work=None, budget=None)
    for start, end in [(1, 100), (10, 90), (20, 80), (30, 70), (40, 60)]:
        context.note_tool_result(
            "read_file", {"path": "a.py", "start": start, "end": end},
            packet(source(start=start, end=end)))
        assert context.begin_round() == 1
    assert context.repeated_source_read_count == 4
    assert context.repeated_request_count == 1
    assert not context.investigation_closed


@pytest.mark.parametrize("separate", ["path", "side", "commit"])
def test_source_read_overlap_is_scoped_to_the_fixed_file_version(separate):
    context = AgentContext(provider=None, work=None, budget=None)
    context.note_tool_result("read_file", {}, packet(source(start=1, end=100)))
    different = source(start=10, end=20)
    if separate == "path":
        different["path"] = "other.py"
    elif separate == "side":
        different["side"] = "base"
    else:
        different["commit_sha"] = "e" * 40
    context.note_tool_result("read_symbol", {"symbol": "target"}, packet(different))
    assert context.repeated_source_read_count == 0


@pytest.mark.parametrize(("second_start", "expected"), [(11, 1), (12, 0)])
def test_only_complete_previous_range_coverage_counts_as_a_repeated_read(second_start, expected):
    context = AgentContext(provider=None, work=None, budget=None)
    for start, end in [(1, 10), (second_start, 20), (5, 15)]:
        context.note_tool_result("read_file", {"start": start, "end": end},
                                 packet(source(start=start, end=end)))
    assert context.repeated_source_read_count == expected


def test_failed_reads_and_search_hints_do_not_establish_received_source_ranges():
    context = AgentContext(provider=None, work=None, budget=None)
    original = packet(source(start=1, end=100))
    context.note_tool_result("read_file", {}, {**original, "ok": False})
    context.note_tool_result("search_code", {}, original)
    context.note_tool_result("read_symbol", {}, packet(source(start=10, end=20)))
    assert context.repeated_source_read_count == 0
    assert len(context.recent_source_reads) == 1
    context.note_tool_result("read_file", {}, packet(source(start=15, end=25)))
    assert context.repeated_source_read_count == 0


def test_source_read_history_is_bounded_and_keeps_recent_ranges():
    context = AgentContext(provider=None, work=None, budget=None)
    for start in range(1, 81):
        context.note_tool_result("read_file", {"start": start, "end": start},
                                 packet(source(start=start, end=start)))
    assert len(context.recent_source_reads) == 64
    context.note_tool_result("read_file", {"start": 1, "end": 1}, packet(source(start=1, end=1)))
    assert context.repeated_source_read_count == 0
    context.note_tool_result("read_file", {"start": 80, "end": 80}, packet(source(start=80, end=80)))
    assert context.repeated_source_read_count == 1


async def test_recording_notes_uses_existing_attempt_authority_and_tool_budget(tmp_path):
    from app.review.tools import record_investigation
    from tests.review.vnext_helpers import setup_source_run

    _, run, _, provider, _ = setup_source_run(tmp_path)
    work = SimpleNamespace(id="work", unit_id=run.plan.units[0].id, kind="review")
    context = AgentContext(provider=provider, work=work, budget=None)

    async def active():
        pass

    context.check_active = active
    note = InvestigationState(question_id="q1", question="Is this caller serialized?",
                             status="resolved", source_refs=["S1"], conclusion="Caller has a lock.")
    runtime = SimpleNamespace(context=context)
    rejected = await record_investigation.coroutine(note=note, runtime=runtime)
    assert rejected["ok"] is False
    assert context.investigations == {}
    ref = SourceRef.model_validate(source())
    context.visible["S1"] = ref
    receipt = await record_investigation.coroutine(note=note, runtime=runtime)
    assert receipt["ok"] is True
    assert context.investigations["q1"] == note
    assert context.resolve(["S1"]) == [ref]
    assert context.tool_calls == 2


async def test_invalid_candidate_source_returns_safe_actionable_error(tmp_path):
    from app.review.domain import CandidateInput
    from app.review.tools import submit_candidate
    from tests.review.vnext_helpers import setup_source_run

    _, run, _, provider, _ = setup_source_run(tmp_path)
    work = SimpleNamespace(id="work", unit_id=run.plan.units[0].id, kind="review")
    context = AgentContext(provider=provider, work=work, budget=None)

    async def active():
        pass

    context.check_active = active
    context.visible["S1"] = SourceRef.model_validate(source())
    result = await submit_candidate.coroutine(proposal=CandidateInput(
        claim="claim", trigger="trigger", impact="impact", source_refs=["S39"]),
        runtime=SimpleNamespace(context=context, tool_call_id="candidate"))
    assert result["error"] == "source_not_visible_in_this_attempt"
    assert result["available_source_refs"] == ["S1"]
    assert "another attempt" in result["next_action"]
    assert not context.submitted and context.tool_calls == 1


@pytest.mark.parametrize("missing", [
    None, "materials", "tests", "consumer", "relations", "unrelated", "large", "verification",
])
def test_small_unit_checkpoint_requires_observed_materials_tests_and_related_consumer(missing):
    unit = Unit(id="unit", paths=["a.py", "tests/test_a.py"], required_hunk_ids=["h1", "h2"],
                risk="low", focus=["correctness"], estimated_input_tokens=100)
    changed = ChangedFile(file_path="a.py", change_type="modified", additions=10, deletions=0)
    context = AgentContext(
        provider=SimpleNamespace(run=SimpleNamespace(
            changed_files=[changed], plan=Plan(units=[unit]))),
        work=SimpleNamespace(unit_id="unit", kind="review"), budget=None)
    context.delivered_hunks = {"h1", "h2"}
    context.source_paths = {"a.py", "tests/test_a.py", "consumer.py"}
    context.related_paths = {"consumer.py"}
    context.investigation_tools = {"search_code"}
    if missing == "materials":
        context.delivered_hunks.remove("h2")
    elif missing == "tests":
        context.source_paths.remove("tests/test_a.py")
    elif missing == "consumer":
        context.source_paths.remove("consumer.py")
    elif missing == "relations":
        context.investigation_tools.clear()
    elif missing == "unrelated":
        context.related_paths.clear()
    elif missing == "large":
        changed.additions = 200
    elif missing == "verification":
        context.work.kind = "verification"
    assert context.small_unit_ready_for_checkpoint() is (missing is None)
