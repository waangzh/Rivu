"""Native tool-calling model factory; LangChain owns the agent loop."""

from abc import ABC, abstractmethod

from app.llm.deepseek import DeepSeekChatModel
from app.llm.receipts import ObservedChatOpenAI
from app.llm.profiles import default_profile
from app.llm.compatible import CompatibleChatModel


class LLMProviderError(ValueError):
    pass


def model_error_message(code):
    return {
        "model_native_tools_required": "该模型不支持审查所需的原生 Tool Calling。",
        "model_reasoning_unsupported": "该模型未配置推理能力。",
        "model_reasoning_effort_unsupported": "该模型不支持所选推理强度。",
        "model_reasoning_cannot_be_disabled": "该推理模型不能关闭 Thinking。",
        "model_reasoning_budget_exceeds_output": "推理 Token 预算必须小于输出上限。",
        "anthropic_effort_incompatible_with_budget_thinking": "此 Anthropic 模型不能同时使用当前推理预算和所选强度。",
        "model_output_limit_exceeded": "输出上限超过模型能力。",
        "model_not_configured": "该 Provider 未配置所选模型。",
        "provider_not_configured": "所选 Provider 未配置。",
        "agent_credentials_unavailable": "所选 Provider 的服务端凭据未配置。",
        "frozen_provider_endpoint_changed": "任务冻结的 Provider 端点已改变，请恢复原配置。",
        "frozen_provider_identity_changed": "任务冻结的 Provider 类型已改变，请恢复原配置。",
        "frozen_provider_unavailable": "任务冻结的 Provider 未配置，请恢复原配置。",
    }.get(code, "模型配置不可用。")


class LLMProvider(ABC):
    @abstractmethod
    def agent_model(self, model=None, *, output_tokens=4096, thinking=None, reasoning_effort=None,
                    profile=None):
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

    def profile(self, model=None):
        from app.core.config import settings
        profile = default_profile(self._provider_name, model or self._default_model,
            context_tokens=settings.rivu_model_context_tokens,
            max_output_tokens=settings.rivu_agent_output_tokens,
            thinking=self._thinking, reasoning_effort=self._reasoning_effort)
        if getattr(self, "_legacy_frozen_profile", False):
            profile = profile.model_copy(update={"supports_tools": True})
        return profile

    def agent_model(self, model=None, *, output_tokens=4096, thinking=None, reasoning_effort=None,
                    profile=None):
        if not self._api_key:
            raise LLMProviderError("agent_credentials_unavailable")
        if profile is None:
            profile = self.profile(model)
            if output_tokens > profile.max_output_tokens:
                profile = profile.model_copy(update={"max_output_tokens": output_tokens})
        enabled = profile.thinking if thinking is None else thinking
        effort = reasoning_effort or profile.reasoning_effort
        validate_model_options(profile, enabled, effort, output_tokens)
        options = dict(api_key=self._api_key, base_url=self._base_url,
            model=profile.model_id, temperature=0.1 if profile.supports_temperature else None,
            max_tokens=output_tokens,
            max_retries=0, timeout=self._request_timeout_seconds)
        if profile.reasoning_format == "deepseek":
            options.update(extra_body={"thinking": {"type": "enabled" if enabled else "disabled"}},
                           use_responses_api=False, stream_usage=True)
            if enabled:
                options.update(temperature=None,
                               reasoning_effort=effort or "high")
            return DeepSeekChatModel(**options)
        if profile.api_protocol == "anthropic":
            from app.llm.native import ObservedChatAnthropic
            if enabled:
                options.update(temperature=None, thinking={"type": "enabled",
                    "budget_tokens": profile.reasoning_budget_tokens})
                if effort:
                    options["effort"] = effort
            return ObservedChatAnthropic(**options)
        if profile.api_protocol == "google":
            from app.llm.native import ObservedChatGoogle
            options.pop("max_tokens")
            options.pop("timeout")
            options.update(max_output_tokens=output_tokens, max_retries=1,
                timeout=self._request_timeout_seconds, include_thoughts=enabled)
            if profile.reasoning_format == "google-budget":
                options["thinking_budget"] = profile.reasoning_budget_tokens if enabled else 0
            elif profile.reasoning_format == "google-level":
                options["thinking_level"] = effort if enabled else "minimal"
            return ObservedChatGoogle(**options)
        options["use_responses_api"] = profile.api_protocol == "openai-responses"
        if options["use_responses_api"]:
            import httpx
            from app.llm.receipts import capture_http_response
            options.update(output_version="responses/v1", store=False,
                include=["reasoning.encrypted_content"],
                http_async_client=httpx.AsyncClient(event_hooks={"response": [capture_http_response]}))
        if enabled and profile.reasoning_format == "openai":
            options["reasoning_effort"] = effort or "high"
        elif profile.reasoning_format == "openai":
            raise LLMProviderError("model_reasoning_cannot_be_disabled")
        if profile.api_protocol == "openai-chat":
            if profile.reasoning_format in {"qwen", "chat-template"}:
                options["extra_body"] = ({"enable_thinking": enabled}
                    if profile.reasoning_format == "qwen" else
                    {"chat_template_kwargs": {"enable_thinking": enabled}})
            elif profile.reasoning_format in {"glm", "kimi"}:
                thinking_options = {"type": "enabled" if enabled else "disabled"}
                options["extra_body"] = {"thinking": thinking_options}
            return CompatibleChatModel(**options, max_tokens_parameter=profile.max_tokens_parameter,
                stream_usage=profile.stream_usage,
                replay_reasoning=enabled and profile.reasoning_format in {"qwen", "chat-template", "glm", "kimi"},
                automatic_tool_choice=enabled and profile.reasoning_format == "kimi",
                model_provider_id=self._provider_name)
        return ObservedChatOpenAI(**options)


def validate_model_options(profile, thinking, effort, output_tokens):
    if not profile.supports_tools:
        raise LLMProviderError("model_native_tools_required")
    if thinking and not profile.reasoning_format:
        raise LLMProviderError("model_reasoning_unsupported")
    if not thinking and profile.reasoning_format == "openai":
        raise LLMProviderError("model_reasoning_cannot_be_disabled")
    if thinking and effort and effort not in profile.reasoning_efforts:
        raise LLMProviderError("model_reasoning_effort_unsupported")
    if output_tokens > profile.max_output_tokens:
        raise LLMProviderError("model_output_limit_exceeded")
    if thinking and profile.reasoning_format == "anthropic" and (
            profile.reasoning_budget_tokens >= output_tokens):
        raise LLMProviderError("model_reasoning_budget_exceeds_output")
    if profile.api_protocol == "anthropic" and thinking and effort in {"max", "xhigh"}:
        raise LLMProviderError("anthropic_effort_incompatible_with_budget_thinking")


def build_provider(provider_name, api_key, base_url, default_model):
    from app.core.config import settings
    normalized = provider_name.strip().lower()
    if normalized not in {"openai", "deepseek", "openai-compatible"}:
        raise ValueError("unsupported_provider")
    return OpenAICompatibleProvider(api_key, base_url, default_model,
        provider_name=normalized, thinking=settings.rivu_deepseek_thinking,
        reasoning_effort=settings.rivu_deepseek_reasoning_effort,
        request_timeout_seconds=settings.rivu_model_request_timeout_seconds)
