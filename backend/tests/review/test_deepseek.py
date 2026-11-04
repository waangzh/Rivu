import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from openai import AsyncOpenAI
from sqlalchemy import select

from app.core.config import settings
from app.llm.providers import OpenAICompatibleProvider, build_provider
from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner, request_input_tokens
from app.review.configuration import freeze_configuration
from app.review.contracts import ReviewRequest
from app.review.domain import Configuration, UnitCompletion
from app.review.orchestrator import ReviewOrchestrator
from tests.review.vnext_helpers import setup_source_run


def deepseek_model(**kwargs):
    return OpenAICompatibleProvider(
        "fixture", "https://example.test/v1", "deepseek-flash",
        provider_name="deepseek", thinking=True).agent_model(**kwargs)


def completion(message, *, reasoning_tokens=7):
    return {"id": "chat", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{
                "index": 0, "finish_reason": "tool_calls", "message": message}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42,
                      "prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 10,
                      "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}}


def tool_message(name, arguments, call_id, reasoning):
    return {"role": "assistant", "content": None, "reasoning_content": reasoning,
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]}


def test_deepseek_configuration_freezes_thinking_and_preserves_existing_tasks(monkeypatch):
    monkeypatch.setattr(settings, "rivu_deepseek_thinking", True)
    monkeypatch.setattr(settings, "rivu_deepseek_reasoning_effort", "low")
    provider = build_provider("deepseek", "fixture", "https://example.test/v1", "deepseek-flash")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), provider)
    assert frozen.thinking is True and frozen.reasoning_effort == "low"
    saved = Configuration.model_validate_json(frozen.model_dump_json())
    provider._thinking, provider._reasoning_effort = False, None
    model = provider.agent_model(thinking=saved.thinking, reasoning_effort=saved.reasoning_effort)
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert model.reasoning_effort == "low" and model.temperature is None
    previous = frozen.model_dump(exclude={"thinking", "reasoning_effort"})
    restored = Configuration.model_validate(previous)
    assert not restored.thinking and restored.reasoning_effort is None
    disabled = provider.agent_model(thinking=restored.thinking)
    assert disabled.extra_body == {"thinking": {"type": "disabled"}}
    plain = build_provider("openai-compatible", "fixture", "https://example.test/v1", "fixture")
    assert not freeze_configuration(
        ReviewRequest(pr_url="https://github.com/o/r/pull/1"), plain).thinking
    assert not plain.agent_model().extra_body


@pytest.mark.parametrize("output_tokens", [2048, 125_000])
async def test_deepseek_http_tool_roundtrip_preserves_reasoning_and_usage(output_tokens):
    model = deepseek_model(output_tokens=output_tokens)
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == output_tokens and "max_completion_tokens" not in payload
        assert "temperature" not in payload
        assert payload["tool_choice"] == "auto"
        if len(requests) > 1:
            assert payload["messages"][1]["reasoning_content"] == "first-synthetic-reasoning"
        if len(requests) == 3:
            assert payload["messages"][3]["reasoning_content"] == "second-synthetic-reasoning"
        return httpx.Response(200, json=completion(tool_message(
            "UnitCompletion", {}, f"call-{len(requests)}",
            "first-synthetic-reasoning" if len(requests) == 1 else "second-synthetic-reasoning")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        bound = model.bind_tools([UnitCompletion], tool_choice="UnitCompletion")
        messages = [HumanMessage(content="fixed-source-fixture")]
        first = await bound.ainvoke(messages)
        assert first.usage_metadata["input_token_details"]["cache_read"] == 20
        assert first.usage_metadata["output_tokens"] == 12
        assert first.usage_metadata["output_token_details"]["reasoning"] == 7
        restored = AIMessage.model_validate_json(first.model_dump_json())
        messages.extend([restored, ToolMessage(content="received", tool_call_id="call-1")])
        second = await bound.ainvoke(messages)
        messages.extend([second, ToolMessage(content="received", tool_call_id="call-2")])
        await bound.ainvoke(messages)
    assert len(requests) == 3
    baseline = request_input_tokens([HumanMessage(content="fixed")], [], UnitCompletion)
    with_reasoning = request_input_tokens([AIMessage(
        content="fixed", additional_kwargs={"reasoning_content": "reasoning " * 2000})],
        [], UnitCompletion)
    assert with_reasoning > baseline + 1500


def test_thinking_does_not_silently_relax_a_multi_tool_forced_choice():
    model = deepseek_model()
    tool = {"type": "function", "function": {
        "name": "first", "parameters": {"type": "object", "properties": {}}}}
    other = {"type": "function", "function": {
        "name": "second", "parameters": {"type": "object", "properties": {}}}}
    with pytest.raises(ValueError, match="deepseek_thinking_forced_tool_unsupported"):
        model._get_request_payload([HumanMessage(content="fixture")], tools=[tool, other],
                                   tool_choice={"type": "function", "function": {"name": "first"}})
    with pytest.raises(ValueError, match="deepseek_reasoning_content_missing"):
        model._get_request_payload([HumanMessage(content="fixture"), AIMessage(content="prior")],
                                   tools=[tool])
    payload = model._get_request_payload([HumanMessage(content="fixture")],
                                         tools=[tool, other], tool_choice="required")
    assert payload["tool_choice"] == "auto" and payload["tools"] == [tool, other]


def test_deepseek_stream_chunks_keep_reasoning_and_top_level_cache_usage():
    model = deepseek_model()
    chunks = []
    for delta in ({"role": "assistant", "reasoning_content": "part-one"},
                  {"reasoning_content": "-two"}, {"content": "answer"}):
        chunks.append(model._convert_chunk_to_generation_chunk({
            "choices": [{"index": 0, "delta": delta}]}, AIMessageChunk, {}))
    chunks.append(model._convert_chunk_to_generation_chunk({
        "choices": [], "usage": completion({})["usage"]}, AIMessageChunk, {}))
    message = chunks[0].message
    for chunk in chunks[1:]:
        message += chunk.message
    assert message.additional_kwargs["reasoning_content"] == "part-one-two"
    assert message.content == "answer"
    assert message.usage_metadata["input_token_details"]["cache_read"] == 20
    assert message.usage_metadata["output_token_details"]["reasoning"] == 7


@pytest.mark.parametrize("complete", [True, False])
def test_stream_terminal_usage_survives_null_delta_without_inventing_missing_counts(complete):
    model = deepseek_model()
    usage = completion({})["usage"] if complete else {
        "completion_tokens_details": {"reasoning_tokens": 5356}}
    result = model._convert_chunk_to_generation_chunk({
        "choices": [{"index": 0, "delta": None}], "usage": usage}, AIMessageChunk, {})
    assert result is not None
    if complete:
        assert result.message.usage_metadata["output_tokens"] == 12
        assert result.message.usage_metadata["input_token_details"]["cache_read"] == 20
    else:
        assert result.message.usage_metadata is None


async def test_deepseek_http_stream_preserves_reasoning_in_following_tool_request():
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["stream"] is True
            chunks = [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "reasoning_content": "stream-reasoning"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "stream-call", "type": "function",
                    "function": {"name": "UnitCompletion", "arguments": "{}"}}]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": completion({})["usage"]},
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(200, text=body + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        assert payload["messages"][1]["reasoning_content"] == "stream-reasoning"
        return httpx.Response(200, json=completion(tool_message(
            "UnitCompletion", {}, "final", "final-reasoning")))

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        bound = model.bind_tools([UnitCompletion])
        messages = [HumanMessage(content="fixture")]
        parts = [part async for part in bound.astream(messages)]
        combined = parts[0]
        for part in parts[1:]:
            combined += part
        restored = AIMessage.model_validate({
            **combined.model_dump(exclude={"type", "tool_call_chunks", "chunk_position"}),
            "type": "ai"})
        assert restored.usage_metadata["output_token_details"]["reasoning"] == 7
        assert restored.usage_metadata["input_token_details"]["cache_read"] == 20
        await bound.ainvoke([*messages, restored, ToolMessage(
            content="received", tool_call_id="stream-call")])
    assert len(requests) == 2


async def test_deepseek_agent_requests_include_reasoning_and_keep_budget_tickets(tmp_path):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    requests, review_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {item["function"]["name"] for item in payload["tools"]}
        if "OverviewOutput" in names:
            name, arguments = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}]}
        else:
            review_requests.append(payload)
            if len(review_requests) == 1:
                name, arguments = "read_file", {
                    "path": "a.py", "revision": "head", "start": 1, "end": 2}
            else:
                prior = [item for item in payload["messages"] if item["role"] == "assistant"]
                assert prior and prior[0]["reasoning_content"] == "synthetic-investigation"
                name, arguments = "UnitCompletion", {}
        return httpx.Response(200, json=completion(tool_message(
            name, arguments, f"call-{len(requests)}", "synthetic-investigation")))

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == "completed" and report.overview.status == "ready"
    assert len(requests) == report.usage["model_calls"] == 3
    assert report.usage["output_tokens"] == 36
    assert report.usage["cached_input_tokens"] == 60
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert all(ticket.state == "settled" for ticket in tickets)
        assert all(ticket.usage["output_token_details"]["reasoning"] == 7 for ticket in tickets)


@pytest.mark.parametrize("known_usage", [False, True])
async def test_thinking_usage_overage_or_unknown_reservation_blocks_further_requests(
        tmp_path, known_usage):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.total_output_tokens = 3 * 4096
    if not known_usage:
        run.configuration.options.limits.total_model_calls = 4
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = completion(tool_message("read_file", {
            "path": "a.py", "revision": "head", "start": 1, "end": 2},
            "investigate", "synthetic-thinking"))
        if known_usage:
            response["usage"].update(completion_tokens=12288, total_tokens=12318,
                                     completion_tokens_details={"reasoning_tokens": 12000})
        else:
            return httpx.Response(503, json={"error": {"message": "fixture"}})
        return httpx.Response(200, json=response)

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.findings == []
    assert report.status in {"partial", "failed"}
    assert len(requests) == report.usage["model_calls"]
    if known_usage:
        assert len(requests) == 1
        assert report.usage["output_tokens"] == 12288
        assert report.usage["committed_output_tokens"] == 12288
        assert report.units[0].error_code == "output_token_limit"
    else:
        assert report.usage["unknown_calls"] == len(requests)
        assert report.usage["output_tokens"] is None
        assert report.usage["committed_output_tokens"] == len(requests) * 4096


@pytest.mark.parametrize("reason", ["failure", "cancelled"])
async def test_deepseek_transport_failures_do_not_retry_or_invent_usage(reason):
    requests = []
    entered = asyncio.Event()

    async def transport(request):
        requests.append(request)
        if reason == "cancelled":
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(503, json={"error": {"message": "fixture"}})

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        from openai import InternalServerError

        with pytest.raises(asyncio.CancelledError if reason == "cancelled" else InternalServerError):
            if reason == "cancelled":
                pending = asyncio.create_task(model.ainvoke("fixture"))
                await entered.wait()
                pending.cancel()
                await pending
            else:
                await model.ainvoke("fixture")
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["tool_arguments", "extra_choice", "structured_output", "replay"])
async def test_http_success_retains_usage_when_response_or_agent_processing_fails(
    tmp_path, failure, caplog,
):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.generate_overview = False
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        if failure == "replay":
            message = tool_message("read_file", {
                "path": "a.py", "start": 1, "end": 2, "revision": "head"}, "read", None)
        else:
            message = tool_message("UnitCompletion", {"unresolved": "invalid-schema"},
                                   f"call-{len(requests)}", "PRIVATE_REASONING_BODY")
        payload = completion(message, reasoning_tokens=5356)
        payload["usage"].update(completion_tokens=5956, total_tokens=5986)
        message["content"] = "PRIVATE_RESPONSE_BODY"
        if failure == "tool_arguments":
            message["tool_calls"][0]["function"]["arguments"] = "[1]"
        elif failure == "extra_choice":
            payload["choices"].append({"index": 1, "message": None, "finish_reason": "stop"})
        return httpx.Response(200, json=payload, headers={"x-request-id": "safe-request-id"})

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    count = 2 if failure == "structured_output" else 1
    assert len(requests) == report.usage["model_calls"] == count
    assert report.usage["unknown_calls"] == 0
    assert report.usage["output_tokens"] == 5956 * count
    assert report.status == "failed" and not report.findings
    diagnostic = report.units[0].error_diagnostic
    assert diagnostic.stage == (
        "request_preparation" if failure == "replay" else
        "agent_processing" if failure == "structured_output" else "response_parsing")
    if failure == "replay":
        assert diagnostic.code == "deepseek_reasoning_content_missing"
    else:
        assert diagnostic.response_received and diagnostic.usage_known
        assert diagnostic.request_id == "safe-request-id"
        assert diagnostic.ticket
    assert "PRIVATE_RESPONSE_BODY" not in caplog.text
    assert "PRIVATE_REASONING_BODY" not in caplog.text
    reopened = store.get(run.id).report
    assert reopened.units[0].error_diagnostic == diagnostic


async def test_usage_without_total_counts_is_unknown_instead_of_zero(tmp_path):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.generate_overview = False
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")

    def transport(request):
        payload = completion(tool_message("UnitCompletion", {}, "done", "reasoning"))
        payload["usage"] = {"completion_tokens_details": {"reasoning_tokens": 5356}}
        return httpx.Response(200, json=payload)

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == "completed"
    assert report.usage["model_calls"] == report.usage["unknown_calls"] == 1
    assert report.usage["input_tokens"] is report.usage["output_tokens"] is None
    assert report.usage["committed_output_tokens"] == run.configuration.options.limits.output_tokens


@pytest.mark.parametrize("corrected", [True, False])
async def test_verifier_schema_correction_is_bounded_and_preserves_successful_response_usage(
    tmp_path, corrected,
):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.generate_overview = False
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    verifier_calls = 0
    requests = []

    def transport(request):
        nonlocal verifier_calls
        payload = json.loads(request.content)
        requests.append(payload)
        packet = json.loads(payload["messages"][1]["content"])
        if "UnitCompletion" in {item["function"]["name"] for item in payload["tools"]}:
            refs = [source["source_ref"] for material in packet["materials"]
                    for source in material["sources"]]
            message = tool_message("submit_candidate", {"proposal": {
                "claim": "零输入除零", "trigger": "x=0", "impact": "抛出异常",
                "source_refs": refs, "location": {"path": "a.py", "line": 2, "side": "head"},
            }}, "candidate", "reviewer-reasoning")
            message["tool_calls"].extend(tool_message("UnitCompletion", {}, "done", "")["tool_calls"])
        else:
            verifier_calls += 1
            refs = [source["source_ref"] for material in packet["materials"]
                    for source in material["sources"]]
            output = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                      "reason": "零值回退被移除。", "source_refs": refs,
                      "author_comment": "零输入会除零，请恢复保护。"}
            if verifier_calls == 1 or not corrected:
                output["verdict"] = "invalid"
            message = tool_message("VerificationOutput", output,
                                   f"verify-{verifier_calls}", "verifier-reasoning")
        return httpx.Response(200, json=completion(message))

    model = deepseek_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert verifier_calls == 2 and len(requests) == report.usage["model_calls"] == 3
    assert report.usage["unknown_calls"] == 0 and report.usage["output_tokens"] == 36
    assert len(report.findings) == (1 if corrected else 0)
    if not corrected:
        work = next(item for item in report.units if item.kind == "verification")
        assert work.error_diagnostic.stage == "agent_processing"
        assert report.candidates[0].verification is None
