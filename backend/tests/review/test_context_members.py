import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage

from app.models.orm import ReviewTaskOrm
from app.models.review.diff import ChangedFile
from app.review.agent import RequestGate
from app.review.budget import BudgetGate
from app.review.context import AgentContext, ContextProvider
from app.review.domain import UnitCompletion
from app.review.evidence import digest
from app.review.planner import identity, members, tokens, unit_capacity
from app.review.tools import read_diff
from app.review.working_context import WorkingContext, packet_message
from tests.review.vnext_helpers import setup_source_run, submit_candidate


def count_members(monkeypatch):
    build = Mock(wraps=members)
    monkeypatch.setattr("app.review.context.members", build)
    return build


async def test_material_exceeding_grouping_capacity_is_delivered_within_model_capacity(tmp_path):
    source = 'DATA = [' + ', '.join('"x"' for _ in range(1200)) + ']\n'
    store, run, _, provider, _ = setup_source_run(tmp_path, head_source=source)
    limits = run.configuration.options.limits
    limits.context_tokens = 32_768
    context = AgentContext(provider, store.work_items(run.id)[0], None)
    key = run.plan.units[0].required_hunk_ids[0]
    packet = await provider.material(context, key)
    assert tokens(packet) > unit_capacity(limits)
    assert tokens(packet) < provider.material_capacity()
    assert context.pending and key in context.pending_hunks


@pytest.mark.asyncio
async def test_initial_and_diff_reads_share_index_across_review_and_verification(tmp_path, monkeypatch):
    store, run, lease, provider, _ = setup_source_run(tmp_path,
        base_source="".join(f"value_{i} = 0\n" for i in range(150)),
        head_source="".join(f"value_{i} = 1\n" for i in range(150)))
    keys = run.plan.units[0].required_hunk_ids
    assert len(keys) > 1
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    candidate = submit_candidate(store, run, lease, reviewer, keys[0])
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    verifier = store.claim(run.id, check.id, lease)
    review_context = AgentContext(provider, reviewer, None)
    verification_context = AgentContext(provider, verifier, None)
    build = count_members(monkeypatch)

    packets = await asyncio.gather(
        provider.initial(review_context),
        provider.initial(verification_context, candidate),
    )
    for packet in packets:
        assert {item["member"]["id"] for item in packet["materials"]} == set(keys)
        assert packet["missing_material"] == {}
    assert packets[1]["candidate_materials"]
    await provider.material(review_context, keys[0])
    await provider.material(verification_context, keys[-1])
    assert build.call_count == 1


@pytest.mark.asyncio
async def test_initial_packet_includes_only_in_unit_relationship_hints(tmp_path):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    unit = run.plan.units[0]
    unit.paths.append("b.py")
    run.plan.relations = [
        (0, "a.py", "b.py"),
        (1, "a.py", "outside.py"),
        (2, "a.py", "a.py"),
    ]

    work = store.work_items(run.id)[0]
    initial = await provider.initial(AgentContext(provider, work, None))

    assert initial["relationship_hints"]["edges"] == [
        {"kind": "import_related", "paths": ["a.py", "b.py"]},
    ]
    assert "not source evidence" in initial["relationship_hints"]["notice"]
    assert "may be incomplete" in initial["relationship_hints"]["notice"]


def test_same_snapshot_reload_reuses_index_and_providers_are_independent(tmp_path, monkeypatch):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    key = run.plan.units[0].required_hunk_ids[0]
    expected = members(run.changed_files)[key]
    build = count_members(monkeypatch)

    assert provider._member(key) == expected
    provider.run = store.begin(run.id, lease)
    assert provider.run is not run
    assert provider._member(key) == expected
    assert build.call_count == 1

    other = ContextProvider(provider.run, provider.evidence, store, lease)
    assert other._member(key) == expected
    assert build.call_count == 2


@pytest.mark.parametrize("changed_input", ["snapshot", "diff"])
def test_cached_index_rejects_replacement_with_different_fixed_inputs(
    tmp_path, monkeypatch, changed_input,
):
    _, run, _, provider, _ = setup_source_run(tmp_path)
    key = run.plan.units[0].required_hunk_ids[0]
    build = count_members(monkeypatch)
    expected = provider._member(key)
    replacement = run.model_copy(deep=True)
    if changed_input == "snapshot":
        replacement.snapshot.head_sha = "c" * 40
    else:
        replacement.changed_files[0].hunks[0].lines[0].content = "changed input"
    provider.run = replacement

    with pytest.raises(ValueError, match="^fixed_snapshot_inputs_changed$"):
        provider._member(key)
    assert build.call_count == 1
    provider.run = run
    assert provider._member(key) == expected


def test_cached_index_rejects_snapshot_mutation(tmp_path):
    _, run, _, provider, _ = setup_source_run(tmp_path)
    key = run.plan.units[0].required_hunk_ids[0]
    provider._member(key)
    run.snapshot.head_sha = "c" * 40

    with pytest.raises(ValueError, match="^fixed_snapshot_inputs_changed$"):
        provider._member(key)


def test_replacing_diff_list_checks_content_without_rebuilding(tmp_path, monkeypatch):
    _, run, _, provider, _ = setup_source_run(tmp_path)
    key = run.plan.units[0].required_hunk_ids[0]
    build = count_members(monkeypatch)
    expected = provider._member(key)
    run.changed_files = deepcopy(run.changed_files)
    assert provider._member(key) == expected
    assert build.call_count == 1
    replacement = deepcopy(run.changed_files)
    replacement[0].hunks[0].lines[0].content = "changed input"
    run.changed_files = replacement

    with pytest.raises(ValueError, match="^fixed_snapshot_inputs_changed$"):
        provider._member(key)


def test_empty_index_is_cached(tmp_path, monkeypatch):
    _, run, _, provider, _ = setup_source_run(tmp_path)
    run.changed_files = []
    build = count_members(monkeypatch)

    for _ in range(2):
        with pytest.raises(KeyError):
            provider._member("missing")
    assert build.call_count == 1


@pytest.mark.asyncio
async def test_material_packets_cannot_mutate_index_or_other_attempts(tmp_path, monkeypatch):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    work = store.work_items(run.id)[0]
    key = run.plan.units[0].required_hunk_ids[0]
    expected = members(run.changed_files)[key]
    first = AgentContext(provider, work, None)
    second = AgentContext(provider, work, None)
    build = count_members(monkeypatch)

    packet = await provider.material(first, key)
    packet["member"]["path"] = "changed.py"
    packet["member"]["lines"][0]["content"] = "changed content"
    packet["member"]["lines"].clear()
    assert second.pending == {} and second.pending_hunks == {}
    other = await provider.material(second, key)
    assert other["member"] == expected
    assert provider._member(key) == expected
    assert first.pending is not second.pending
    assert build.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mismatch", "capacity", "unavailable"])
async def test_cached_members_still_require_source_checks_and_capacity(tmp_path, monkeypatch, failure):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    work = store.work_items(run.id)[0]
    key = run.plan.units[0].required_hunk_ids[0]
    await provider.material(AgentContext(provider, work, None), key)
    original_read = provider.evidence.read

    async def read(*args):
        result = await original_read(*args)
        if failure == "mismatch":
            result = {**result, "content": "mismatched\n" * 10}
        return result

    checked_read = AsyncMock(side_effect=OSError("unavailable") if failure == "unavailable" else read)
    monkeypatch.setattr(provider.evidence, "read", checked_read)
    if failure == "capacity":
        monkeypatch.setattr(provider, "material_capacity", lambda: 1)
    context = AgentContext(provider, work, None)
    error = OSError if failure == "unavailable" else ValueError
    reason = {"mismatch": "diff_blob_mismatch", "capacity": "material_capacity",
              "unavailable": "unavailable"}[failure]

    with pytest.raises(error, match=f"^{reason}$"):
        await provider.material(context, key)
    assert checked_read.await_count > 0
    assert context.pending == {} and context.pending_hunks == {}


@pytest.mark.asyncio
async def test_structural_metadata_is_read_per_packet_without_polluting_index(tmp_path, monkeypatch):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    run.changed_files = [ChangedFile(file_path="new.py", old_file_path="a.py",
        change_type="renamed", additions=0, deletions=0)]
    key, expected = next(iter(members(run.changed_files).items()))
    tree = Mock(side_effect=lambda *args: {args[-1][0]: {"mode": "100644", "type": "blob"}})
    monkeypatch.setattr(provider.evidence.git, "get_tree_entries", tree)
    work = store.work_items(run.id)[0]
    build = count_members(monkeypatch)

    for _ in range(2):
        packet = await provider.material(AgentContext(provider, work, None), key)
        assert "a.py" in packet["member"]["base_entry"]
        assert "new.py" in packet["member"]["head_entry"]
        assert packet["sources"] == []
        assert provider._member(key) == expected
    assert tree.call_count == 4
    assert build.call_count == 1


async def test_model_projection_encodes_alignment_without_repeating_fixed_source(tmp_path):
    _, _, _, provider, _ = setup_source_run(tmp_path,
        base_source="BEGIN = 0\nx = 'old'\nKEEP = 1\nLAST = 2\n",
        head_source="BEGIN = 0\nx = 'new'\ny = 'extra'\nKEEP = 1\nLAST = 2\n")
    run = provider.run
    work = provider.store.work_items(run.id)[0]
    key = run.plan.units[0].required_hunk_ids[0]
    context = AgentContext(provider, work, None)
    packet = await provider.material(context, key)
    original = packet_message({"materials": [packet]}, "initial")
    projected, = context.compact_messages([original])
    material, = json.loads(projected.content)["materials"]
    assert material["sources"] == packet["sources"]
    assert material["member"]["id"] == key
    assert "lines" not in material["member"]
    assert tokens(projected.content) < tokens(original.content)
    restored = []
    for kind, old, new, count in material["member"]["line_runs"]:
        restored.extend((kind, old + offset if old is not None else None,
                         new + offset if new is not None else None) for offset in range(count))
    assert restored == [(line["kind"], line["old_line_no"], line["new_line_no"])
                        for line in packet["member"]["lines"]]
    assert context.pending_hunks == {key: identity(packet)}
    assert json.loads(original.content)["materials"] == [packet]
    one_side = packet_message({"materials": [{**material, "sources": material["sources"][1:]}]},
                              "one-side")
    pending, hunks = context.projected_delivery([one_side], [original])
    assert set(pending) == {material["sources"][1]["source_ref"]}
    assert hunks == {}

    raw = {"ok": True, **packet}
    first, second = [await context.archive_tool("read_diff", {"hunk_id": key}, ToolMessage(
        content=json.dumps(raw), name="read_diff", tool_call_id=f"diff-{index}")) for index in range(2)]
    first_projection, second_projection = context.compact_messages([first, second])
    assert json.loads(first_projection.content)["member"] == material["member"]
    assert json.loads(second_projection.content)["same_result_as_tool_call"] == "diff-0"
    assert json.loads(second_projection.content)["receipt_ref"] == "R2"
    assert "member" not in json.loads(second_projection.content)
    assert json.loads(context.compact_messages([second])[0].content)["member"] == material["member"]


async def test_pending_reference_needs_its_handle_and_matching_projected_bytes(tmp_path):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    context = AgentContext(provider, store.work_items(run.id)[0], None)
    broad = await context.read("a.py", 1, 2, "head")
    narrow = await context.read("a.py", 2, 2, "head")
    wide_message = packet_message({"working_set": [broad]}, "wide")
    narrow_message = packet_message({"working_set": [narrow]}, "narrow")
    projection = context.compact_messages([wide_message, narrow_message])
    assert json.loads(projection[-1].content)["working_set"][0]["content_omitted"]
    pending, _ = context.projected_delivery(projection, [wide_message, narrow_message])
    assert set(pending) == {broad["source_ref"], narrow["source_ref"]}
    pending, _ = context.projected_delivery([projection[-1]], [wide_message, narrow_message])
    assert pending == {}

    for change in ({"side": "base"}, {"commit_sha": run.snapshot.base_sha}, {"path": "other.py"}):
        wrong = {**broad, "source": {**broad["source"], **change}}
        other = packet_message({"working_set": [wrong]}, "other")
        pending, _ = context.projected_delivery([other, projection[-1]], [wide_message, narrow_message])
        assert pending == {}
    invalid_content = "def unrelated(x):\n    return x\n"
    wrong = {**broad, "content": invalid_content,
             "source": {**broad["source"], "content_hash": digest(invalid_content)}}
    other = packet_message({"working_set": [wrong]}, "other")
    pending, _ = context.projected_delivery([other, projection[-1]], [wide_message, narrow_message])
    assert pending == {}


@pytest.mark.parametrize("capacity", ["window", "reservation", "structure_only"])
async def test_request_gate_delivers_only_projected_material_and_read_diff_repairs_coverage(
    tmp_path, capacity,
):
    def source(value):
        return ("# fixed context\n" * 10).join(
            f"VALUES_{index} = [" + f"'{value}'," * 500 + "]\n" for index in range(4))

    before = source("old")
    after = before if capacity == "structure_only" else source("new")
    store, run, lease, provider, sessions = setup_source_run(
        tmp_path, base_source=before, head_source=after,
        support_sources={"mode.py": "VALUE = 1\n"}, head_executable_paths=["mode.py"])
    all_members = members(run.changed_files)
    structural_key, = [key for key, member in all_members.items() if not member["lines"]]
    unit = run.plan.units[0]
    unit.required_hunk_ids = list(all_members)
    unit.paths = sorted({item["path"] for item in all_members.values()})
    limits = run.configuration.options.limits
    limits.context_tokens = 65_536
    run.configuration.options.generate_overview = False
    limits.total_model_calls = None
    limits.total_input_tokens = None
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    context = AgentContext(provider, work, BudgetGate(store, run, lease), window=WorkingContext())
    context.initial_packet = await provider.initial(context)
    staged = dict(context.pending_hunks)
    original = context.initial_packet["materials"]
    assert set(staged) == set(unit.required_hunk_ids)
    # Model or cumulative budget becomes tighter after the initial packet is assembled.
    if capacity == "window":
        limits.context_tokens = 13_312
    elif capacity == "structure_only":
        limits.context_tokens = 8192
        context.initial_packet["already_submitted"] = [{"claim": "background " * 8000}]
    else:
        limits.total_input_tokens = 12_000
        context.initial_packet["already_submitted"] = [{"claim": "background " * 8000}]
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    history = [packet_message(context.initial_packet, "initial")]
    request = ModelRequest(model=None, messages=history, tools=[read_diff],
        response_format=ToolStrategy(UnitCompletion), runtime=SimpleNamespace(context=context))
    sent = []

    async def receive(request):
        sent.append(request)
        return ModelResponse(result=[AIMessage(content="fixture", usage_metadata={
            "input_tokens": 100, "output_tokens": 1, "total_tokens": 101})])

    middleware = RequestGate()
    await middleware.awrap_model_call(request, receive)
    assert context.window.epoch == 1
    working = next(json.loads(message.content)["working_materials"] for message in sent[-1].messages
                   if message.id.startswith("working-set-"))
    structure = next(packet for packet in original if packet["member"]["id"] == structural_key)
    assert structure["member"]["base_entry"]["mode.py"]["mode"] == "100644"
    assert structure["member"]["head_entry"]["mode.py"]["mode"] == "100755"
    assert structure in working
    assert context.window.metrics["tools_unchanged"] is False
    delivered = {packet["member"]["id"] for packet in working}
    missing = set(staged) - delivered
    saved, = store.work_items(run.id)
    if capacity == "structure_only":
        assert saved.material_delivery == "complete"
        assert delivered == {structural_key}
        assert saved.delivered_materials == staged
        assert not context.visible
        return
    assert missing
    assert saved.material_delivery == "incomplete"
    assert set(saved.delivered_hunk_ids) == delivered
    assert saved.delivered_materials == {key: staged[key] for key in delivered}
    assert saved.missing_material_reasons == dict.fromkeys(missing, "material_capacity")
    assert context.material_failures == saved.missing_material_reasons
    memory = next(json.loads(message.content) for message in sent[-1].messages
                  if message.id.startswith("investigation-memory-"))
    assert memory["missing_material"] == saved.missing_material_reasons
    assert not context.pending_hunks and not context.pending
    assert set(context.visible) == {source["source_ref"] for packet in working for source in packet["sources"]}
    assert set(saved.visible_source_ids) == {ref.id for ref in context.visible.values()}

    if capacity == "reservation":
        assert context.investigation_closed
        assert not sent[-1].tools
        return

    for key in sorted(missing):
        if context.window.checkpoint_pending:
            context.memory_revision += 1
            context.window.checkpoint_revision = None
        result = await read_diff.coroutine(hunk_id=key, runtime=request.runtime)
        assert result["ok"] is True
        call_id = f"reread-{key}"
        history = [*history, AIMessage(content="", tool_calls=[
            {"id": call_id, "name": "read_diff", "args": {"hunk_id": key}},
        ]), await context.archive_tool("read_diff", {"hunk_id": key}, ToolMessage(
            content=json.dumps(result), name="read_diff", tool_call_id=call_id))]
        await middleware.awrap_model_call(request.override(messages=history), receive)
        saved, = store.work_items(run.id)
        assert key in saved.delivered_hunk_ids and key not in saved.missing_material_reasons
        assert all(item["source_ref"] in context.visible for item in result["sources"])
    assert saved.material_delivery == "complete"
    assert not context.material_failures
