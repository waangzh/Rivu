import asyncio
import json
from datetime import timedelta

import pytest
import httpx
from langchain_core.messages import AIMessage, ToolMessage
from openai import AsyncOpenAI
from sqlalchemy import select

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.budget import BudgetGate
from app.review.domain import Run, utcnow
from app.review.report import build_report, generate_overview
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped
from tests.review.vnext_helpers import setup_source_run

PRIVATE = "PRIVATE_PROVIDER_BODY_SOURCE_AND_REASONING"
USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def response(mode="valid", *, usage=USAGE):
    arguments = {"change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
                 "finding_order": []}
    calls = [{"id": "overview", "name": "OverviewOutput", "args": arguments}]
    invalid = []
    if mode == "prose":
        calls = []
    elif mode == "arguments":
        arguments["change_overview"] = []
    elif mode == "source":
        arguments["change_overview"][0]["source_refs"] = [PRIVATE]
    elif mode == "finding":
        arguments["finding_order"] = [PRIVATE]
    elif mode == "tool":
        calls[0]["name"] = PRIVATE
    elif mode == "multiple":
        calls.append({**calls[0], "id": "extra"})
    elif mode == "malformed":
        calls = []
        invalid = [{"id": "broken", "name": "OverviewOutput", "args": "{",
                    "error": PRIVATE, "type": "invalid_tool_call"}]
    elif mode == "missing_id":
        calls[0]["id"] = None
        arguments["change_overview"] = []
    return AIMessage(content=PRIVATE, tool_calls=calls, invalid_tool_calls=invalid,
                     additional_kwargs={"reasoning_content": PRIVATE},
                     response_metadata={"finish_reason": PRIVATE}, usage_metadata=usage)


class OverviewModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def setup_overview(tmp_path, **limits):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    for key, value in limits.items():
        setattr(run.configuration.options.limits, key, value)
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    report = build_report(run, store.work_items(run.id), [], store.usage(run.id))
    return store, run, lease, provider, sessions, report


@pytest.mark.parametrize(("mode", "code"), [
    ("prose", "overview_schema"), ("arguments", "overview_schema"),
    ("source", "overview_unknown_source"), ("finding", "overview_unknown_finding"),
    ("tool", "overview_schema"), ("multiple", "overview_schema"),
    ("malformed", "overview_schema"),
])
async def test_overview_corrects_once_with_original_history_and_safe_diagnostics(tmp_path, caplog, mode, code):
    store, run, lease, provider, sessions, report = setup_overview(tmp_path)
    first = response(mode)
    model = OverviewModel(first, response())
    with caplog.at_level("INFO", logger="Rivu.Review.Overview"):
        overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "ready" and overview.error_code is None
    assert [item.error_code for item in overview.diagnostics] == [code, None]
    diagnostic = overview.diagnostics[0]
    assert diagnostic.content_present and diagnostic.reasoning_present
    assert diagnostic.finish_reason == "other"
    assert diagnostic.unknown_source_refs == int(mode == "source")
    assert diagnostic.unknown_finding_ids == int(mode == "finding")
    assert diagnostic.validation_errors > 0 if mode == "arguments" else diagnostic.validation_errors == 0
    assert len(model.requests) == 2
    correction = model.requests[1]
    assert correction[:2] == model.requests[0]
    assert correction[2] is first
    assert correction[2].additional_kwargs["reasoning_content"] == PRIVATE
    returned_ids = {call["id"] for call in [*first.tool_calls, *first.invalid_tool_calls]}
    assert {item.tool_call_id for item in correction if isinstance(item, ToolMessage)} == returned_ids
    assert "Runtime correction" in correction[-1].content
    assert PRIVATE not in correction[-1].content
    assert store.usage(run.id)["model_calls"] == 2
    assert store.usage(run.id)["output_tokens"] == 100
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm).where(ReviewRequestOrm.kind == "overview")))
        assert len(tickets) == 2 and all(ticket.state == "settled" for ticket in tickets)
        assert tickets[1].reserved_input_tokens > tickets[0].reserved_input_tokens
    report.overview = overview
    store.save_report(run.id, report, lease)
    restored = Run.model_validate_json(store.get(run.id).model_dump_json())
    assert restored.report.overview.diagnostics == overview.diagnostics
    assert PRIVATE not in overview.model_dump_json() and PRIVATE not in caplog.text


@pytest.mark.parametrize(("mode", "code"), [
    ("prose", "overview_schema"), ("source", "overview_unknown_source"),
    ("finding", "overview_unknown_finding"),
])
async def test_overview_rejects_second_invalid_result_without_third_request(tmp_path, mode, code):
    store, run, lease, provider, _, report = setup_overview(tmp_path)
    model = OverviewModel(response(mode), response(mode))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "failed" and overview.error_code == code
    assert overview.output is None and overview.sources == []
    assert len(overview.diagnostics) == len(model.requests) == 2
    assert store.usage(run.id)["unknown_calls"] == 0


async def test_saved_failed_correction_is_reused_without_new_requests(tmp_path):
    store, run, lease, provider, _, report = setup_overview(tmp_path)
    model = OverviewModel(response("source"), response("source"))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    report.overview = overview
    store.save_report(run.id, report, lease)
    provider.run = store.get(run.id)
    restored = await generate_overview(report, provider, BudgetGate(store, provider.run, lease), model)
    assert restored == overview
    assert len(model.requests) == store.usage(run.id)["model_calls"] == 2


async def test_unreplayable_tool_response_fails_without_corrupting_history(tmp_path):
    store, run, lease, provider, _, report = setup_overview(tmp_path)
    model = OverviewModel(response("missing_id"))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "failed" and overview.error_code == "overview_schema"
    assert len(model.requests) == 1


@pytest.mark.parametrize(("resource", "code"), [
    ("calls", "model_call_limit"), ("input", "input_token_limit"),
    ("output", "output_token_limit"), ("unknown", "output_token_limit"),
    ("context", "context_capacity"),
])
async def test_overview_correction_stays_within_original_shared_budgets(tmp_path, resource, code):
    limits = {"total_model_calls": 3} if resource == "calls" else (
        {"total_input_tokens": 40_000} if resource == "input" else
        {"context_tokens": 8192} if resource == "context" else {"total_output_tokens": 4096})
    store, run, lease, provider, _, report = setup_overview(tmp_path, **limits)
    budget = BudgetGate(store, run, lease)
    if resource == "calls":
        for _ in range(2):
            ticket = await budget.reserve(None, 100, overview=True)
            await budget.settle(ticket, USAGE)
    first = response("prose", usage=None if resource == "unknown" else USAGE)
    if resource == "input":
        first.usage_metadata = {**USAGE, "input_tokens": 30_000, "total_tokens": 30_050}
    elif resource == "output":
        first.usage_metadata = {**USAGE, "output_tokens": 4096, "total_tokens": 4196}
    elif resource == "context":
        first.additional_kwargs["reasoning_content"] = "reasoning " * 10_000
    model = OverviewModel(first)
    overview = await generate_overview(report, provider, budget, model)
    assert overview.status == "failed" and overview.error_code == code
    assert len(model.requests) == len(overview.diagnostics) == 1
    assert overview.diagnostics[0].error_code == "overview_schema"
    usage = store.usage(run.id)
    assert usage["model_calls"] == (3 if resource == "calls" else 1)
    assert usage["unknown_calls"] == int(resource == "unknown")
    if resource == "unknown":
        assert usage["output_tokens"] is None and usage["committed_output_tokens"] == 4096


@pytest.mark.parametrize("stop", [asyncio.CancelledError, LeaseLost, ReviewStopped])
async def test_overview_correction_propagates_cancellation_and_lease_loss(tmp_path, stop):
    store, run, lease, provider, _, report = setup_overview(tmp_path)
    model = OverviewModel(response("prose"), stop())
    with pytest.raises(stop):
        await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert len(model.requests) == 2
    assert store.usage(run.id)["model_calls"] == 2
    assert store.usage(run.id)["unknown_calls"] == 1
    assert len(store.get(run.id).report.overview.diagnostics) == 1


@pytest.mark.parametrize("mode", ["invalid", "valid"])
async def test_overview_response_after_deadline_cannot_be_accepted_or_corrected(tmp_path, mode):
    store, run, lease, provider, _, report = setup_overview(tmp_path)

    class LateModel(OverviewModel):
        async def ainvoke(self, messages):
            result = await super().ainvoke(messages)
            run.hard_deadline = utcnow() - timedelta(seconds=1)
            return result

    model = LateModel(response("prose" if mode == "invalid" else "valid"))
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "skipped_deadline" and overview.error_code == "hard_deadline"
    assert overview.output is None and len(model.requests) == 1
    assert store.usage(run.id)["output_tokens"] == 50


@pytest.mark.parametrize("correcting", [False, True])
@pytest.mark.parametrize("error", [ValueError(PRIVATE), TimeoutError(PRIVATE)])
async def test_overview_transport_errors_are_safe_and_do_not_get_format_correction(
        tmp_path, caplog, error, correcting):
    store, run, lease, provider, _, report = setup_overview(tmp_path)
    model = OverviewModel(*([response("prose")] if correcting else []), error)
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "failed"
    assert overview.error_code == ("overview_timeout" if isinstance(error, TimeoutError) else "overview_unavailable")
    assert len(model.requests) == 1 + int(correcting)
    assert store.usage(run.id)["unknown_calls"] == 1
    assert len(overview.diagnostics) == int(correcting)
    assert PRIVATE not in overview.model_dump_json() and PRIVATE not in caplog.text


async def test_disabled_overview_does_not_build_materials_or_spend_budget(tmp_path, monkeypatch):
    store, run, lease, provider, sessions, report = setup_overview(tmp_path)
    run.configuration.options.generate_overview = False
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")

    def forbidden(*args, **kwargs):
        pytest.fail("disabled overview must not prepare materials")

    monkeypatch.setattr("app.review.report.overview_material", forbidden)
    overview = await generate_overview(report, provider, BudgetGate(store, run, lease), None)
    assert overview.status == "skipped_disabled" and overview.error_code is None
    assert store.usage(run.id)["model_calls"] == 0
    with pytest.raises(BudgetExceeded, match="overview_disabled"):
        await BudgetGate(store, run, lease).reserve(None, 100, overview=True)


@pytest.mark.parametrize("mode", ["prose", "source", "malformed"])
async def test_overview_correction_preserves_deepseek_thinking_http_roundtrip(tmp_path, mode):
    from tests.review.test_deepseek import completion, deepseek_model, tool_message

    store, run, lease, provider, sessions, report = setup_overview(tmp_path)
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "enabled"} and payload["tool_choice"] == "auto"
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["OverviewOutput"]
        if len(requests) == 1:
            message = tool_message("OverviewOutput", {
                "change_overview": [{"text": "调整计算。", "source_refs": ["invented"]}],
            }, "first", PRIVATE)
            if mode == "prose":
                message.pop("tool_calls")
                message["content"] = PRIVATE
            elif mode == "malformed":
                message["tool_calls"][0]["function"]["arguments"] = "{"
        else:
            assert len(requests) == 2
            prior = next(item for item in payload["messages"] if item["role"] == "assistant")
            assert prior["reasoning_content"] == PRIVATE
            receipts = [item for item in payload["messages"] if item["role"] == "tool"]
            assert [item["tool_call_id"] for item in receipts] == ([] if mode == "prose" else ["first"])
            message = tool_message("OverviewOutput", {
                "change_overview": [{"text": "调整计算。", "source_refs": ["S1"]}],
            }, "second", "corrected")
        return httpx.Response(200, json=completion(message))

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        overview = await generate_overview(report, provider, BudgetGate(store, run, lease), model)
    assert overview.status == "ready"
    assert store.usage(run.id)["model_calls"] == 2
    assert store.usage(run.id)["output_tokens"] == 24
    assert store.usage(run.id)["cached_input_tokens"] == 40
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert all(ticket.state == "settled" for ticket in tickets)
        assert all(ticket.usage["output_token_details"]["reasoning"] == 7 for ticket in tickets)
