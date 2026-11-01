import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.review.diff import ChangedFile
from app.review.context import AgentContext, ContextProvider
from app.review.planner import members
from tests.review.vnext_helpers import setup_source_run, submit_candidate


def count_members(monkeypatch):
    build = Mock(wraps=members)
    monkeypatch.setattr("app.review.context.members", build)
    return build


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
