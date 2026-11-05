import asyncio
import json
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select

from app.llm.providers import OpenAICompatibleProvider
from app.models.orm import ReviewRequestOrm, ReviewTaskOrm, WorkerJobOrm
from app.review.agent import AgentRunner
from app.review.domain import UnitCompletion, utcnow
from app.review.incremental import refine_plan
from app.review.orchestrator import ReviewOrchestrator
from app.review.report import build_report
from app.review.store import OVERVIEW_INPUT, LeaseLost, ReviewStopped
from tests.review.vnext_helpers import completion_gap, setup_source_run


@pytest.mark.parametrize("payload", [
    {"unresolved_reason": "已完成检查，只剩未证实的测试加载疑点。"},
    {"unresolved": "未排除测试可能失败。"},
    {"unresolved": {"missing_premise": "未证实的测试失败。"}},
    {"unresolved": completion_gap(source_refs=[])},
    {"unresolved": {**completion_gap(), "why_required": "  "}},
])
def test_completion_rejects_free_text_and_incomplete_gaps(payload):
    with pytest.raises(ValidationError):
        UnitCompletion.model_validate(payload)


def test_completed_investigation_has_no_gap_and_live_schema_has_no_legacy_field():
    assert UnitCompletion().model_dump() == {"unresolved": None}
    assert "unresolved_reason" not in UnitCompletion.model_json_schema()["properties"]
    gap = completion_gap()
    assert UnitCompletion(unresolved=gap).unresolved.model_dump() == gap


def invalid_completion(kind):
    if kind == "legacy":
        return {"unresolved_reason": "已完成检查，未发现缺陷；仅未排除测试加载可能失败。"}
    if kind == "missing_fields":
        return {"unresolved": {"missing_premise": "模块加载是否会失败。"}}
    refs = {"unknown": ["S999"], "receipt": ["R1"], "duplicate": ["S1", "S1"]}[kind]
    return {"unresolved": completion_gap(source_refs=refs)}


async def run_completion_fixture(tmp_path, *, initial=None, corrected=None, stop=None,
                                 call_limit=48, thinking=False, resource=None):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = call_limit
    # This fixture exercises one attempt's schema correction, not orchestration followups.
    run.configuration.options.gap_followups = 0
    if resource == "calls":
        run.configuration.options.limits.total_model_calls = 3
    elif resource == "output":
        run.configuration.options.limits.total_output_tokens = 3 * 4096
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, reviews = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            reviews.append(payload)
            if len(reviews) == 1:
                name, result = "UnitCompletion", initial or {}
            else:
                assert len(reviews) == 2
                assert "read_file" not in names
                if thinking:
                    assistants = [message for message in payload["messages"]
                                  if message["role"] == "assistant"]
                    assert assistants[0]["reasoning_content"] == "synthetic-reasoning"
                if stop == "failure":
                    return httpx.Response(503, json={"error": {"message": "PRIVATE_PROVIDER_BODY"}})
                if stop == "timeout":
                    raise httpx.ReadTimeout("PRIVATE_PROVIDER_BODY", request=request)
                if stop in {"cancelled", "lease_lost", "deadline"}:
                    with sessions.begin() as session:
                        if stop == "cancelled":
                            session.get(ReviewTaskOrm, run.id).status = "cancelled"
                        elif stop == "lease_lost":
                            session.get(WorkerJobOrm, lease["job_id"]).lease_owner = "other"
                        else:
                            row = session.get(ReviewTaskOrm, run.id)
                            row.task_snapshot = {**row.task_snapshot,
                                "hard_deadline": (utcnow() - timedelta(seconds=1)).isoformat()}
                if stop == "reads":
                    name, result = "read_file", {
                        "path": "a.py", "revision": "head", "start": 1, "end": 2}
                elif stop == "prose":
                    return httpx.Response(200, json={
                        "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {
                            "role": "assistant", "content": "All checks complete."}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
                    })
                else:
                    name, result = "UnitCompletion", corrected or {}
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call-{len(requests)}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(result)},
        }]}
        if thinking:
            message["reasoning_content"] = "synthetic-reasoning"
        input_tokens = run.configuration.options.limits.total_input_tokens if resource == "input" else 100
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": 5,
                      "total_tokens": input_tokens + 5},
        })

    model = OpenAICompatibleProvider(
        "fixture", "https://example.test/v1", "fixture",
        provider_name="deepseek" if thinking else "openai-compatible",
        thinking=thinking).agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        error = None
        try:
            report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
        except (LeaseLost, ReviewStopped, asyncio.CancelledError) as exc:
            error, report = exc, None
    return store, run, sessions, report, reviews, requests, error


@pytest.mark.parametrize("invalid", ["legacy", "missing_fields", "unknown", "receipt", "duplicate"])
async def test_completion_misuse_can_be_corrected_once_without_a_false_partial(tmp_path, invalid):
    store, run, sessions, report, reviews, requests, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion(invalid))
    assert error is None
    assert report.status == "completed"
    assert report.units[0].result == {"unresolved": None}
    assert not report.reasons and not report.findings
    assert len(reviews) == 2 and len(requests) == report.usage["model_calls"] == 3
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert len(tickets) == 3 and all(ticket.state == "settled" for ticket in tickets)
    assert store.usage(run.id)["unknown_calls"] == 0


async def test_completion_correction_preserves_thinking_roundtrip(tmp_path):
    _, _, _, report, reviews, _, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("legacy"), thinking=True)
    assert error is None and report.status == "completed"
    assert len(reviews) == 2


@pytest.mark.parametrize("corrected", [False, True])
async def test_a_sourced_gap_is_separate_from_execution_completion(tmp_path, corrected):
    gap = {"unresolved": completion_gap()}
    _, _, _, report, reviews, _, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("legacy") if corrected else gap, corrected=gap)
    assert error is None
    assert report.status == "completed" and report.coverage == "complete"
    assert report.unresolved_unit_count == 1
    assert report.units[0].result["unresolved"] == gap["unresolved"]
    assert report.units[0].result["unresolved_sources"]
    assert report.units[0].has_unresolved_investigation
    assert report.reasons == [f"{report.units[0].unit_id}:unresolved_investigation"]
    assert len(reviews) == (2 if corrected else 1)


@pytest.mark.parametrize("stop", ["failure", "timeout", "reads", "prose", "rejected_again"])
async def test_failed_completion_correction_cannot_turn_into_a_clean_result(tmp_path, stop, caplog):
    _, _, _, report, reviews, requests, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("legacy"),
        corrected=invalid_completion("unknown") if stop == "rejected_again" else None, stop=stop)
    assert error is None
    assert len(reviews) == 2 and report.status != "completed"
    assert report.units[0].status == "failed"
    assert report.units[0].result is None and not report.findings
    assert report.usage["model_calls"] == len(requests)
    if stop in {"failure", "timeout"}:
        assert report.usage["unknown_calls"] == 1
    assert "PRIVATE_PROVIDER_BODY" not in caplog.text


async def test_completion_correction_cannot_bypass_the_agent_call_budget(tmp_path):
    _, _, _, report, reviews, _, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("legacy"), call_limit=1)
    assert error is None
    assert len(reviews) == 1 and report.status != "completed"
    assert report.units[0].error_code == "agent_model_limit"


@pytest.mark.parametrize(("resource", "error_code"), [
    ("calls", "model_call_limit"), ("input", "input_token_limit"), ("output", "output_token_limit"),
])
async def test_completion_correction_cannot_bypass_shared_budget_protections(tmp_path, resource, error_code):
    _, _, _, report, reviews, _, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("legacy"), resource=resource)
    assert error is None and len(reviews) == 1
    assert report.status != "completed"
    assert report.units[0].error_code == error_code
    assert report.units[0].result is None
    if resource == "input":
        assert report.usage["committed_input_tokens"] >= OVERVIEW_INPUT


@pytest.mark.parametrize("stop", ["cancelled", "lease_lost", "deadline"])
async def test_completion_correction_preserves_cancellation_lease_and_deadline(tmp_path, stop):
    store, run, _, report, reviews, _, error = await run_completion_fixture(
        tmp_path, initial=invalid_completion("unknown"), stop=stop)
    assert len(reviews) == 2
    if stop in {"cancelled", "lease_lost"}:
        assert isinstance(error, ReviewStopped if stop == "cancelled" else LeaseLost)
    else:
        assert error is None and report.status != "completed"
    saved = store.work_items(run.id)[0]
    assert saved.result is None and saved.status != "completed"
    assert not store.candidates(run.id)


@pytest.mark.parametrize("result", [
    {"unresolved": completion_gap()},
    {"unresolved_reason": "已完成检查，但保留历史的未证实测试疑点。"},
])
async def test_structured_and_archived_gaps_remain_partial_and_cannot_be_reused(tmp_path, result):
    store, run, lease, _, _ = setup_source_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    store.deliver(run.id, work, run.plan.units[0].required_hunk_ids, {}, lease)
    store.finish_work(run.id, work, "completed", result, None, lease)
    saved = store.work_items(run.id)[0]
    assert saved.result == result
    report = build_report(run, [saved], [], store.usage(run.id))
    assert report.status == "completed" and report.unresolved_unit_count == 1 and report.reasons
    previous = run.model_copy(deep=True)
    previous.snapshot.head_sha = "c" * 40
    previous.report = report
    git = Mock()
    git.changed_paths_between.return_value = {"README.md"}
    git.get_tree_entries.return_value = {"a.py": {"oid": "unchanged"}}
    refine_plan(run, previous, [saved], git, tmp_path, [])
    assert run.plan.incremental_from is None
    assert all(unit.reused_from is None for unit in run.plan.units)
