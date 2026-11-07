"""Resolve frozen model configuration using server-owned credential references."""

import os
from pathlib import Path
from dotenv import dotenv_values

from app.llm.profiles import ProviderConfig
from app.llm.providers import LLMProviderError, OpenAICompatibleProvider, build_provider


class RegisteredProvider(OpenAICompatibleProvider):
    def __init__(self, provider_id, config, api_key, timeout):
        super().__init__(api_key, config.base_url, config.default_model,
                         provider_name=config.provider, request_timeout_seconds=timeout)
        self.provider_id = provider_id
        self.config = config

    def profile(self, model=None):
        identifier = model or self._default_model
        for profile in self.config.models:
            if profile.model_id == identifier:
                return profile
        raise LLMProviderError("model_not_configured")

    def agent_model(self, model=None, *, profile=None, **kwargs):
        return super().agent_model(model, profile=profile or self.profile(model), **kwargs)


class ProviderRegistry:
    def __init__(self, settings):
        self.settings = settings

    @property
    def default_id(self):
        return self.settings.rivu_default_provider if self.settings.rivu_model_providers else "default"

    def configs(self):
        if self.settings.rivu_model_providers:
            return self.settings.rivu_model_providers
        provider = build_provider(self.settings.rivu_provider, self.settings.openai_api_key,
                                  self.settings.openai_base_url, self.settings.rivu_model)
        return {"default": ProviderConfig(provider=provider._provider_name,
            base_url=provider._base_url, api_key_env="OPENAI_API_KEY",
            default_model=provider._default_model, models=[provider.profile()])}

    def resolve(self, provider_id=None, *, endpoint=None, api_key_env=None):
        identifier = provider_id or self.default_id
        config = self.configs().get(identifier)
        if config is None:
            raise LLMProviderError("provider_not_configured")
        if endpoint is not None and config.base_url != endpoint:
            raise LLMProviderError("frozen_provider_endpoint_changed")
        if api_key_env and api_key_env != config.api_key_env:
            config = config.model_copy(update={"api_key_env": api_key_env})
        key = (self.settings.openai_api_key if config.api_key_env == "OPENAI_API_KEY"
               else os.environ.get(config.api_key_env) or dotenv_values(
                   Path(__file__).resolve().parents[2] / ".env",
                   encoding="utf-8-sig").get(config.api_key_env))
        if not self.settings.rivu_model_providers:
            provider = build_provider(config.provider, key, config.base_url, config.default_model)
            provider.provider_id = identifier
            return provider
        return RegisteredProvider(identifier, config, key,
                                  self.settings.rivu_model_request_timeout_seconds)

    def for_run(self, configuration):
        if configuration.provider_id is not None:
            provider = self.resolve(configuration.provider_id, endpoint=configuration.endpoint,
                                    api_key_env=configuration.api_key_env)
        else:
            matches = [identifier for identifier, config in self.configs().items()
                       if config.provider == configuration.provider
                       and config.base_url == configuration.endpoint]
            if len(matches) == 1:
                provider = self.resolve(matches[0], endpoint=configuration.endpoint,
                                         api_key_env=configuration.api_key_env)
            else:
                provider = build_provider(configuration.provider, self.settings.openai_api_key,
                    configuration.endpoint, configuration.model)
                if configuration.model_profile is None:
                    provider._legacy_frozen_profile = True
        if provider._provider_name != configuration.provider:
            raise LLMProviderError("frozen_provider_identity_changed")
        return provider
