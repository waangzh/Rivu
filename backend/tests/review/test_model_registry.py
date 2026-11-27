import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import AsyncOpenAI

from app.core.config import Settings
from app.llm.profiles import ModelProfile, ProviderConfig, default_profile
from app.llm.registry import ProviderRegistry
from app.llm.providers import LLMProviderError
from app.llm.receipts import observe_response
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
    second = ModelProfile(model_id="second", max_tokens_parameter="max_completion_tokens",
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
    assert new.prompt_version == "review-vnext-16"
    historical = Configuration.model_validate_json(saved.model_copy(
        update={"prompt_version": "review-vnext-15"}).model_dump_json())
    assert historical.prompt_version == "review-vnext-15"
    assert new.provider_id == "cloud" and new.thinking
    payload = providers.for_run(new).agent_model(profile=new.model_profile)._get_request_payload([
        HumanMessage(content="fixture")])
    assert payload["reasoning_effort"] == "high" and "temperature" not in payload
    assert payload["max_completion_tokens"] == new.options.limits.output_tokens


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


@pytest.mark.parametrize("model_id", ["o1", "o3", "o4-mini", "gpt-5"])
def test_openai_reasoning_defaults_are_chat_compatible(model_id):
    profile = default_profile("openai", model_id)
    assert profile.thinking and profile.reasoning_format == "openai"
    assert profile.reasoning_effort in profile.reasoning_efforts
    assert profile.max_tokens_parameter == "max_completion_tokens"
    assert not profile.supports_temperature and profile.stream_usage


@pytest.mark.parametrize("model_id", ["gpt-5", "gpt-5-mini", "gpt-5-nano-2025-08-07"])
def test_original_gpt_5_family_offers_only_its_supported_reasoning_efforts(model_id):
    profile = default_profile("openai", model_id)
    assert profile.reasoning_efforts == ["minimal", "low", "medium", "high"]
    assert profile.reasoning_effort == "medium"


async def test_openai_reasoning_uses_chat_completions_for_tool_roundtrip(monkeypatch):
    model_id = "o3"
    profile = default_profile("openai", model_id)
    model = registry(monkeypatch, openai=("openai", profile)).resolve().agent_model()
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/chat/completions"
        assert payload["reasoning_effort"] == profile.reasoning_effort
        assert payload["max_completion_tokens"] == 4096
        assert "max_tokens" not in payload and "temperature" not in payload
        if len(requests) == 2:
            assert payload["messages"][1]["tool_calls"][0]["id"] == "call-fixture"
            assert payload["messages"][2]["tool_call_id"] == "call-fixture"
        return httpx.Response(200, json={"id": "chat-fixture", "object": "chat.completion",
            "created": 1, "model": model_id, "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call-fixture", "type": "function",
                     "function": {"name": "UnitCompletion", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 7}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        bound = model.bind_tools([UnitCompletion])
        with observe_response() as receipt:
            first = await bound.ainvoke([HumanMessage(content="fixture")])
        assert receipt.received and receipt.parsed
        assert first.usage_metadata == receipt.usage == {
            "input_tokens": 30, "output_tokens": 12, "total_tokens": 42,
            "input_token_details": {"cache_read": 20}, "output_token_details": {"reasoning": 7},
        }
        restored = AIMessage.model_validate_json(first.model_dump_json())
        await bound.ainvoke([HumanMessage(content="fixture"), restored,
                            ToolMessage(content="received", tool_call_id="call-fixture")])
    assert len(requests) == 2


@pytest.mark.parametrize(("provider", "protocol", "format_"), [
    ("anthropic", "anthropic", "anthropic"),
    ("google", "google", "google-budget"),
    ("google", "google", "google-level"),
    ("openai", "openai-responses", "openai"),
    ("openai-compatible", "openai-responses", None),
    ("openai", None, "google-level"),
    ("anthropic", None, None),
])
def test_retired_frozen_configuration_cannot_resolve_or_construct_model(monkeypatch, provider, protocol, format_):
    providers = registry(monkeypatch, active=("openai", default_profile("openai", "gpt-4.1-mini")))
    payload = {"provider": provider, "provider_id": "missing", "endpoint": "https://example.test",
               "model": "fixture", "options": {}}
    if protocol or format_:
        payload["model_profile"] = {
            "model_id": "fixture", "api_protocol": protocol, "reasoning_format": format_,
            "reasoning_budget_tokens": 2048,
        }
    saved = Configuration.model_validate(payload)
    assert saved.model_dump()["model_profile"] == payload.get("model_profile")
    with pytest.raises(LLMProviderError, match="^unsupported_provider$"):
        providers.for_run(saved)
    if saved.model_profile is not None:
        with pytest.raises(LLMProviderError, match="^unsupported_provider$"):
            providers.resolve().agent_model(profile=saved.model_profile)


def test_historical_chat_profile_remains_runnable(monkeypatch):
    profile = ModelProfile(model_id="first", reasoning_format="deepseek", thinking=True,
                           max_tokens_parameter="max_completion_tokens", stream_usage=False)
    providers = registry(monkeypatch, local=("openai-compatible", profile))
    saved = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), providers)
    payload = saved.model_dump()
    payload["model_profile"].update(api_protocol="openai-chat", reasoning_budget_tokens=2048)
    restored = Configuration.model_validate(payload)
    model = providers.for_run(restored).agent_model(profile=restored.model_profile)
    assert model.compat.max_tokens_parameter == "max_tokens" and model.stream_usage
    assert model.compat.strict_reasoning_replay


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


def test_format_specific_reasoning_levels_are_validated():
    with pytest.raises(ValueError, match="model_reasoning_effort_format_mismatch"):
        ModelProfile(model_id="deepseek", reasoning_format="deepseek", reasoning_efforts=["medium"])
