import pytest

from app.api import system
from app.core.config import settings
from app.llm.profiles import ModelProfile, ProviderConfig
from app.models.operations import ModelConnectionRequest, SystemSettingsUpdate


def provider_config():
    return ProviderConfig(provider="openai-compatible", base_url="https://example.test/v1",
        api_key_env="TEST_MODEL_KEY", default_model="fixture",
        models=[ModelProfile(model_id="fixture")])


async def test_registry_api_returns_capabilities_and_key_reference_not_key(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "private-key-fixture")
    monkeypatch.setattr(settings, "rivu_model_providers", {"local": provider_config()})
    monkeypatch.setattr(settings, "rivu_default_provider", "local")
    result = await system.get_model_providers()
    assert result.default_provider == "local"
    assert result.providers[0].credentials_configured
    assert "private-key-fixture" not in result.model_dump_json()
    assert result.providers[0].config.models[0].supports_tools
    catalog = await system.get_available_models()
    assert catalog.models[0].profile.model_id == "fixture"


async def test_settings_persists_registry_and_changes_default_without_changing_keys(monkeypatch):
    monkeypatch.setattr(settings, "rivu_model_providers", {})
    monkeypatch.setattr(settings, "rivu_default_provider", None)
    monkeypatch.setattr(settings, "rivu_retention_days", settings.rivu_retention_days)
    writes, reloads = [], []
    monkeypatch.setattr(system, "persist_editable_settings", lambda value: writes.append(value))
    class Service:
        def reload_provider(self, value):
            reloads.append(value)
    monkeypatch.setattr(system, "get_review_service", lambda: Service())
    async def diagnostics():
        return "diagnostics"
    monkeypatch.setattr(system, "get_system_diagnostics", diagnostics)
    request = SystemSettingsUpdate(model_providers={"local": provider_config()}, default_provider="local")
    assert await system.update_system_settings(request) == "diagnostics"
    assert settings.rivu_default_provider == "local"
    assert reloads[0].default_id == "local"
    assert set(writes[0]) == {"RIVU_MODEL_PROVIDERS", "RIVU_DEFAULT_PROVIDER"}
    assert '"api_key_env": "TEST_MODEL_KEY"' in writes[0]["RIVU_MODEL_PROVIDERS"]


async def test_connection_probe_checks_native_tool_result_and_reports_usage(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.llm.registry import ProviderRegistry
    requested = []
    class Bound:
        async def ainvoke(self, messages):
            requested.extend(messages)
            return AIMessage(content="", tool_calls=[{"name": "ConnectionCheck",
                "args": {"connected": True}, "id": "check"}], usage_metadata={
                    "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    class Provider:
        def profile(self, model):
            return ModelProfile(model_id="fixture")
        def agent_model(self, model, **kwargs):
            class Model:
                def bind_tools(self, tools):
                    assert tools[0].__name__ == "ConnectionCheck"
                    return Bound()
            return Model()
    monkeypatch.setattr(ProviderRegistry, "resolve", lambda self, provider: Provider())
    result = await system.test_model_connection("local", ModelConnectionRequest(model="fixture"))
    assert result.status == "connected" and result.usage["output_tokens"] == 5
    assert len(requested) == 1 and "ConnectionCheck" in requested[0].content


def test_provider_configuration_requires_default_model_and_safe_endpoint():
    with pytest.raises(ValueError, match="provider_requires_unique_models_and_valid_default"):
        ProviderConfig(provider="openai", base_url="https://example.test", api_key_env="TEST_KEY",
                       default_model="other", models=[ModelProfile(model_id="fixture")])
