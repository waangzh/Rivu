import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from openai import AsyncOpenAI

from app.models.orm import ReviewTaskOrm
from app.llm.presets import provider_presets
from app.llm.registry import RegisteredProvider
from app.review.domain import UnitCompletion
from app.review.agent import AgentRunner
from app.review.orchestrator import ReviewOrchestrator
from tests.review.vnext_helpers import setup_source_run


def test_presets_use_standard_api_and_supported_models():
    presets = {item["id"]: item for item in provider_presets()}
    assert set(presets) == {"openai", "deepseek", "glm", "kimi"}
    assert presets["deepseek"]["config"].base_url == "https://api.deepseek.com"
    assert presets["glm"]["config"].base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert presets["kimi"]["config"].base_url == "https://api.moonshot.ai/v1"
    for preset in presets.values():
        assert all(profile.api_protocol.startswith("openai-")
                   for profile in preset["config"].models)
        assert preset["config"].default_model in {
            profile.model_id for profile in preset["config"].models}


@pytest.mark.parametrize("provider_id", ["glm", "kimi"])
async def test_official_thinking_and_tool_history_roundtrip(provider_id):
    preset = next(item for item in provider_presets() if item["id"] == provider_id)
    provider = RegisteredProvider(provider_id, preset["config"], "fixture", 60)
    model = provider.agent_model()
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"]["type"] == "enabled"
        assert payload["model"] == preset["config"].default_model
        if provider_id == "kimi":
            assert "temperature" not in payload
            assert payload["tool_choice"] == "auto"
        if len(requests) > 1:
            assert payload["messages"][1]["reasoning_content"] == "fixture reasoning"
        return httpx.Response(200, json={"id": "chat-fixture", "object": "chat.completion",
            "created": 1, "model": payload["model"], "choices": [{"index": 0,
            "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "reasoning_content": "fixture reasoning", "tool_calls": [{
                    "id": "call-fixture", "type": "function", "function": {
                        "name": "UnitCompletion", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url=preset["config"].base_url,
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        bound = model.bind_tools([UnitCompletion], tool_choice="UnitCompletion")
        first = await bound.ainvoke([HumanMessage(content="fixture")])
        assert first.additional_kwargs["reasoning_content"] == "fixture reasoning"
        assert first.usage_metadata["output_tokens"] == 12
        await bound.ainvoke([HumanMessage(content="fixture"), first,
                            ToolMessage(content="received", tool_call_id="call-fixture")])
    assert len(requests) == 2


def test_kimi_does_not_silently_change_a_multi_tool_forced_choice():
    preset = next(item for item in provider_presets() if item["id"] == "kimi")
    model = RegisteredProvider("kimi", preset["config"], "fixture", 60).agent_model()
    tools = [{"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": {}}}} for name in ("first", "second")]
    with pytest.raises(ValueError, match="thinking_forced_tool_unsupported"):
        model._get_request_payload([HumanMessage(content="fixture")], tools=tools,
            tool_choice={"type": "function", "function": {"name": "first"}})


@pytest.mark.parametrize("provider_id", ["glm", "kimi"])
def test_thinking_can_be_disabled_for_the_preset(provider_id):
    preset = next(item for item in provider_presets() if item["id"] == provider_id)
    model = RegisteredProvider(provider_id, preset["config"], "fixture", 60).agent_model(thinking=False)
    payload = model._get_request_payload([HumanMessage(content="fixture")])
    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert not model.replay_reasoning


@pytest.mark.parametrize("provider_id", ["glm", "kimi"])
async def test_streaming_reasoning_is_replayed_with_usage(provider_id):
    preset = next(item for item in provider_presets() if item["id"] == provider_id)
    model = RegisteredProvider(provider_id, preset["config"], "fixture", 60).agent_model()

    def transport(request):
        payload = json.loads(request.content)
        if not payload["stream"]:
            assert payload["messages"][1]["reasoning_content"] == "stream reasoning"
            return httpx.Response(200, json={"id": "chat", "object": "chat.completion",
                "created": 1, "model": payload["model"], "choices": [{"index": 0,
                "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}})
        if provider_id == "kimi":
            assert payload["stream_options"]["include_usage"] is True
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant",
                "reasoning_content": "stream "}}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "reasoning"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call-stream",
                "type": "function", "function": {"name": "UnitCompletion", "arguments": "{}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, text=body + "data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url=preset["config"].base_url,
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        bound = model.bind_tools([UnitCompletion])
        parts = [chunk async for chunk in bound.astream([HumanMessage(content="fixture")])]
        message = parts[0]
        for chunk in parts[1:]:
            message += chunk
        assert message.additional_kwargs["reasoning_content"] == "stream reasoning"
        assert message.usage_metadata["output_tokens"] == 12
        from langchain_core.messages import AIMessage
        restored = AIMessage.model_validate({
            **message.model_dump(exclude={"type", "tool_call_chunks", "chunk_position"}), "type": "ai"})
        await bound.ainvoke([HumanMessage(content="fixture"), restored,
                            ToolMessage(content="received", tool_call_id="call-stream")])


@pytest.mark.parametrize("provider_id", ["glm", "kimi"])
async def test_thinking_tool_loop_runs_through_review_budget(tmp_path, provider_id):
    store, run, lease, context, sessions = setup_source_run(tmp_path)
    run.configuration.options.generate_overview = False
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    preset = next(item for item in provider_presets() if item["id"] == provider_id)
    model = RegisteredProvider(provider_id, preset["config"], "fixture", 60).agent_model()
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            name, args = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
        else:
            previous = [message for message in payload["messages"] if message["role"] == "assistant"]
            assert previous[0]["reasoning_content"] == "fixture reasoning"
            name, args = "UnitCompletion", {}
        return httpx.Response(200, json={"id": "chat-fixture", "object": "chat.completion",
            "created": 1, "model": payload["model"], "choices": [{"index": 0,
            "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "reasoning_content": "fixture reasoning", "tool_calls": [{
                    "id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}}]}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url=preset["config"].base_url,
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, context, lease)
    assert report.status == "completed"
    assert len(requests) == report.usage["model_calls"] == 2
    assert report.usage["output_tokens"] == 24
