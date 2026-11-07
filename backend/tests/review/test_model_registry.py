import asyncio
import json

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from google.genai import Client, types
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import AsyncOpenAI

from app.core.config import Settings
from app.llm.profiles import ModelProfile, ProviderConfig
from app.llm.profiles import default_profile
from app.llm.registry import ProviderRegistry
from app.llm.receipts import capture_http_response, observe_response
from app.review.configuration import freeze_configuration
from app.review.contracts import ReviewRequest
from app.review.domain import Configuration, Options, UnitCompletion


def registry(monkeypatch, **profiles):
    monkeypatch.setenv("TEST_MODEL_KEY", "fixture")
    configs = {identifier: ProviderConfig(provider=provider, base_url="https://example.test",
        api_key_env="TEST_MODEL_KEY", default_model=profile.model_id, models=[profile])
        for identifier, (provider, profile) in profiles.items()}
    return ProviderRegistry(Settings(_env_file=None, rivu_model_providers=configs,
        rivu_default_provider=next(iter(configs)), rivu_github_publish_enabled=False))


def test_task_freezes_profile_and_resolves_original_provider_after_default_changes(monkeypatch):
    first = ModelProfile(model_id="first", context_tokens=32768, max_output_tokens=2048)
    second = ModelProfile(model_id="second", api_protocol="openai-responses",
        reasoning_format="openai", thinking=True, reasoning_efforts=["low", "high"],
        reasoning_effort="high", supports_temperature=False)
    providers = registry(monkeypatch, local=("openai-compatible", first), cloud=("openai", second))
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1")
    saved = Configuration.model_validate_json(freeze_configuration(request, providers).model_dump_json())
    assert saved.provider_id == "local" and saved.model_profile == first
    assert saved.api_key_env == "TEST_MODEL_KEY"
    assert saved.options.limits.context_tokens == 32768
    assert saved.options.limits.output_tokens == 2048
    providers.settings.rivu_default_provider = "cloud"
    replacement = first.model_copy(update={"context_tokens": 65536, "max_output_tokens": 4096})
    providers.settings.rivu_model_providers["local"] = providers.configs()["local"].model_copy(
        update={"models": [replacement]})
    original = providers.for_run(saved)
    model = original.agent_model(saved.model, profile=saved.model_profile,
                                 output_tokens=saved.options.limits.output_tokens)
    assert model.model_name == "first" and model.max_tokens == 2048
    new = freeze_configuration(request, providers)
    assert new.provider_id == "cloud" and new.thinking
    payload = providers.for_run(new).agent_model(profile=new.model_profile)._get_request_payload([
        HumanMessage(content="fixture")])
    assert payload["reasoning"]["effort"] == "high" and "temperature" not in payload
    assert payload["store"] is False and "reasoning.encrypted_content" in payload["include"]


def test_legacy_task_resolves_its_frozen_endpoint_after_global_endpoint_changes():
    settings = Settings(_env_file=None, openai_api_key="fixture",
        openai_base_url="https://new.example.test/v1",
        rivu_model_providers={"new": ProviderConfig(provider="openai-compatible",
            base_url="https://new.example.test/v1", api_key_env="TEST_MODEL_KEY",
            default_model="new-model", models=[ModelProfile(model_id="new-model")])},
        rivu_default_provider="new", rivu_github_publish_enabled=False)
    providers = ProviderRegistry(settings)
    saved = Configuration(provider="openai", endpoint="https://old.example.test/v1",
        model="historical-model", options=Options())
    model = providers.for_run(saved).agent_model()
    assert model.openai_api_base == "https://old.example.test/v1"
    assert model.model_name == "historical-model"


def test_model_selection_freezes_thinking_and_native_tool_requirement(monkeypatch):
    profile = ModelProfile(model_id="qwen", reasoning_format="qwen", thinking=True)
    providers = registry(monkeypatch, gateway=("openai-compatible", profile))
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1", thinking=False)
    frozen = freeze_configuration(request, providers)
    assert frozen.thinking is False
    model = providers.for_run(frozen).agent_model(profile=frozen.model_profile, thinking=False)
    payload = model._get_request_payload([HumanMessage(content="fixture")])
    assert payload["extra_body"] == {"enable_thinking": False}
    assert payload["max_tokens"] == 4096 and "max_completion_tokens" not in payload
    providers.settings.rivu_model_providers["gateway"] = providers.configs()["gateway"].model_copy(
        update={"models": [profile.model_copy(update={"supports_tools": False})]})
    with pytest.raises(ValueError, match="model_native_tools_required"):
        freeze_configuration(request, providers)


async def test_anthropic_http_roundtrip_keeps_thinking_signature_and_cached_usage(monkeypatch):
    profile = ModelProfile(model_id="claude-sonnet", api_protocol="anthropic",
        reasoning_format="anthropic", thinking=True, supports_temperature=False)
    providers = registry(monkeypatch, claude=("anthropic", profile))
    model = providers.resolve().agent_model()
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/messages"
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert payload["max_tokens"] == 4096 and "temperature" not in payload
        if len(requests) == 2:
            assert payload["messages"][1]["content"][0]["signature"] == "fixture-signature"
        return httpx2.Response(200, json={"id": "msg-fixture", "type": "message",
            "role": "assistant", "model": "claude-sonnet", "stop_reason": "tool_use",
            "stop_sequence": None, "content": [
                {"type": "thinking", "thinking": "fixture-reasoning", "signature": "fixture-signature"},
                {"type": "tool_use", "id": "call-fixture", "name": "UnitCompletion", "input": {}}],
            "usage": {"input_tokens": 10, "output_tokens": 12,
                      "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model.__dict__["_async_client"] = AsyncAnthropic(api_key="fixture",
            base_url="https://example.test", http_client=client, max_retries=0)
        bound = model.bind_tools([UnitCompletion])
        with observe_response() as receipt:
            first = await bound.ainvoke([HumanMessage(content="fixture")])
        assert receipt.parsed and receipt.usage["input_tokens"] == 35
        assert first.usage_metadata["input_token_details"] == {"cache_read": 20, "cache_creation": 5}
        restored = AIMessage.model_validate_json(first.model_dump_json())
        await bound.ainvoke([HumanMessage(content="fixture"), restored,
                            ToolMessage(content="received", tool_call_id="call-fixture")])
    assert len(requests) == 2


async def test_google_http_roundtrip_keeps_thought_signature_and_reasoning_usage(monkeypatch):
    profile = ModelProfile(model_id="gemini-2.5-flash", api_protocol="google",
        reasoning_format="google-budget", thinking=True)
    providers = registry(monkeypatch, gemini=("google", profile))
    model = providers.resolve().agent_model()
    assert model.max_retries == 1
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path.endswith(":generateContent")
        assert payload["generationConfig"]["thinkingConfig"]["thinking_budget"] == 2048
        if len(requests) == 2:
            assert payload["contents"][1]["parts"][0]["thoughtSignature"] == "c2lnbmF0dXJl"
        return httpx.Response(200, json={"candidates": [{"index": 0, "finishReason": "STOP",
            "content": {"role": "model", "parts": [{"functionCall": {"name": "UnitCompletion",
                "args": {}}, "thoughtSignature": "c2lnbmF0dXJl"}]}}],
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 5,
                "thoughtsTokenCount": 7, "totalTokenCount": 42, "cachedContentTokenCount": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = Client(api_key="fixture", http_options=types.HttpOptions(
            base_url="https://example.test", httpx_async_client=client))
        model.client = sdk
        bound = model.bind_tools([UnitCompletion])
        with observe_response() as receipt:
            first = await bound.ainvoke([HumanMessage(content="fixture")])
        assert receipt.parsed and receipt.usage["output_tokens"] == 12
        assert first.usage_metadata["output_token_details"]["reasoning"] == 7
        restored = AIMessage.model_validate_json(first.model_dump_json())
        await bound.ainvoke([HumanMessage(content="fixture"), restored,
                            ToolMessage(content="received", tool_call_id=first.tool_calls[0]["id"])])
    await sdk.aio.aclose()
    assert len(requests) == 2


async def test_responses_http_roundtrip_preserves_encrypted_reasoning_and_usage(monkeypatch):
    profile = ModelProfile(model_id="o3", api_protocol="openai-responses", reasoning_format="openai",
        thinking=True, reasoning_effort="high", reasoning_efforts=["low", "high"],
        supports_temperature=False)
    model = registry(monkeypatch, openai=("openai", profile)).resolve().agent_model()
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/responses"
        assert payload["max_output_tokens"] == 4096
        if len(requests) == 2:
            assert payload["input"][1]["encrypted_content"] == "encrypted-fixture"
        return httpx.Response(200, json={"id": "resp-fixture", "object": "response",
            "created_at": 1, "model": "o3", "status": "completed", "error": None,
            "incomplete_details": None, "parallel_tool_calls": True, "tools": [],
            "tool_choice": "auto", "output": [
                {"id": "rs-fixture", "type": "reasoning", "summary": [],
                 "encrypted_content": "encrypted-fixture"},
                {"type": "function_call", "name": "UnitCompletion", "arguments": "{}",
                 "call_id": "call-fixture", "id": "fc-fixture", "status": "completed"}],
            "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens_details": {"reasoning_tokens": 7}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport),
            event_hooks={"response": [capture_http_response]}) as client:
        model.root_async_client = AsyncOpenAI(api_key="fixture", base_url="https://example.test",
                                             http_client=client, max_retries=0)
        bound = model.bind_tools([UnitCompletion])
        with observe_response() as receipt:
            first = await bound.ainvoke([HumanMessage(content="fixture")])
        assert receipt.parsed and receipt.usage["output_token_details"]["reasoning"] == 7
        restored = AIMessage.model_validate_json(first.model_dump_json())
        await bound.ainvoke([HumanMessage(content="fixture"), restored,
                            ToolMessage(content="received", tool_call_id="call-fixture")])
    assert len(requests) == 2


async def test_distinct_providers_can_generate_concurrently(monkeypatch):
    profile = ModelProfile(model_id="first")
    providers = registry(monkeypatch, first=("openai-compatible", profile),
        second=("openai-compatible", profile.model_copy(update={"model_id": "second"})))
    received = []

    async def transport(request):
        payload = json.loads(request.content)
        received.append(payload["model"])
        await asyncio.sleep(0)
        return httpx.Response(200, json={"id": "chat-fixture", "object": "chat.completion",
            "created": 1, "model": payload["model"], "choices": [{"index": 0,
            "finish_reason": "stop", "message": {"role": "assistant", "content": "fixture"}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        models = [providers.resolve(identifier).agent_model() for identifier in providers.configs()]
        for model in models:
            sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test", http_client=client)
            model.root_async_client, model.async_client = sdk, sdk.chat.completions
        results = await asyncio.gather(*(model.ainvoke("fixture") for model in models))
    assert set(received) == {"first", "second"}
    assert all(result.usage_metadata["output_tokens"] == 12 for result in results)


def test_unknown_openai_model_does_not_assume_native_tool_support():
    assert not default_profile("openai", "unknown-model").supports_tools
    assert default_profile("openai", "gpt-4.1-mini").supports_tools


def test_provider_rejects_a_model_using_another_native_protocol():
    with pytest.raises(ValueError, match="provider_protocol_mismatch"):
        ProviderConfig(provider="anthropic", base_url="https://example.test",
            api_key_env="TEST_MODEL_KEY", default_model="fixture",
            models=[ModelProfile(model_id="fixture", api_protocol="openai-chat")])


def test_protocol_specific_reasoning_levels_are_validated():
    with pytest.raises(ValueError, match="model_reasoning_effort_protocol_mismatch"):
        ModelProfile(model_id="gemini", api_protocol="google", reasoning_format="google-level",
            reasoning_efforts=["low", "xhigh"])
    with pytest.raises(ValueError, match="model_reasoning_effort_protocol_mismatch"):
        ModelProfile(model_id="deepseek", reasoning_format="deepseek", reasoning_efforts=["medium"])
