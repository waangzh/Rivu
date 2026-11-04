"""Native tool-calling model factory; LangChain owns the agent loop."""

from abc import ABC, abstractmethod

from app.llm.deepseek import DeepSeekChatModel
from app.llm.receipts import ObservedChatOpenAI


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    def agent_model(self, model=None, *, output_tokens=4096, thinking=None, reasoning_effort=None):
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key, base_url, default_model, *,
                 provider_name="openai-compatible", request_timeout_seconds=60,
                 thinking=False, reasoning_effort="high"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._thinking = thinking if provider_name == "deepseek" else False
        self._reasoning_effort = reasoning_effort if self._thinking else None
        self._provider_name = provider_name
        self._request_timeout_seconds = request_timeout_seconds

    def agent_model(self, model=None, *, output_tokens=4096, thinking=None, reasoning_effort=None):
        if not self._api_key:
            raise LLMProviderError("agent_credentials_unavailable")
        options = dict(api_key=self._api_key, base_url=self._base_url,
            model=model or self._default_model, temperature=0.1, max_tokens=output_tokens,
            max_retries=0, timeout=self._request_timeout_seconds)
        if self._provider_name == "deepseek":
            enabled = self._thinking if thinking is None else thinking
            options.update(extra_body={"thinking": {"type": "enabled" if enabled else "disabled"}},
                           use_responses_api=False, stream_usage=True)
            if enabled:
                options.update(temperature=None,
                               reasoning_effort=reasoning_effort or self._reasoning_effort or "high")
            return DeepSeekChatModel(**options)
        return ObservedChatOpenAI(**options)


def build_provider(provider_name, api_key, base_url, default_model):
    from app.core.config import settings
    normalized = provider_name.strip().lower()
    if normalized not in {"openai", "deepseek", "openai-compatible"}:
        raise ValueError("unsupported_provider")
    return OpenAICompatibleProvider(api_key, base_url, default_model,
        provider_name=normalized, thinking=settings.rivu_deepseek_thinking,
        reasoning_effort=settings.rivu_deepseek_reasoning_effort,
        request_timeout_seconds=settings.rivu_model_request_timeout_seconds)
