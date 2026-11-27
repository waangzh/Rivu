"""Provider identity and model capabilities for Chat Completions."""

import re
from typing import Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderKind = Literal["openai", "deepseek", "openai-compatible"]
SUPPORTED_PROVIDERS = frozenset(get_args(ProviderKind))
ReasoningFormat = Literal["openai", "deepseek", "glm", "kimi", "qwen", "chat-template"]
ReasoningEffort = Literal["minimal", "low", "medium", "high", "max", "xhigh"]
MaxTokensParameter = Literal["max_tokens", "max_completion_tokens"]


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(min_length=1, max_length=200)
    supports_tools: bool = True
    reasoning_format: ReasoningFormat | None = None
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list)
    thinking: bool = False
    reasoning_effort: ReasoningEffort | None = None
    supports_temperature: bool = True
    max_tokens_parameter: MaxTokensParameter = "max_tokens"
    stream_usage: bool = False
    context_tokens: int = Field(default=128_000, ge=8192, le=1_000_000)
    max_output_tokens: int = Field(default=4096, ge=256, le=125_000)

    @model_validator(mode="after")
    def validate_reasoning(self):
        if self.thinking and not self.reasoning_format:
            raise ValueError("model_reasoning_unsupported")
        if self.reasoning_effort and self.reasoning_effort not in self.reasoning_efforts:
            raise ValueError("model_reasoning_effort_unsupported")
        if self.reasoning_format == "deepseek" and not set(self.reasoning_efforts) <= {"low", "high", "max"}:
            raise ValueError("model_reasoning_effort_format_mismatch")
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("model_output_exceeds_context")
        return self


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderKind
    base_url: str
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    default_model: str
    models: list[ModelProfile] = Field(min_length=1)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("provider_endpoint_requires_plain_http_url")
        return value.rstrip("/")

    @model_validator(mode="after")
    def valid_models(self):
        identifiers = [profile.model_id for profile in self.models]
        if len(set(identifiers)) != len(identifiers) or self.default_model not in identifiers:
            raise ValueError("provider_requires_unique_models_and_valid_default")
        return self


def default_profile(provider, model_id, *, context_tokens=128_000, max_output_tokens=4096,
                    thinking=False, reasoning_effort=None):
    values = dict(model_id=model_id, context_tokens=context_tokens,
                  max_output_tokens=max_output_tokens, supports_tools=provider != "openai")
    if provider == "openai":
        from langchain_openai.data._profiles import _PROFILES
        metadata = _PROFILES.get(model_id, {})
        values.update(max_tokens_parameter="max_completion_tokens", stream_usage=True,
                      context_tokens=min(context_tokens, metadata.get("max_input_tokens") or context_tokens),
                      max_output_tokens=min(max_output_tokens, metadata.get("max_output_tokens") or max_output_tokens),
                      supports_tools=metadata.get("tool_calling", False),
                      supports_temperature=metadata.get("temperature", True))
    else:
        metadata = {}
    if provider == "deepseek":
        values.update(reasoning_format="deepseek", stream_usage=True, reasoning_efforts=["low", "high", "max"],
                      thinking=thinking, reasoning_effort=reasoning_effort if thinking else None)
    elif provider == "openai" and (metadata.get("reasoning_output") or (
            not metadata and model_id.startswith(("o1", "o3", "o4", "gpt-5"))
            and "chat" not in model_id)):
        efforts = metadata.get("reasoning_effort_levels") or ["low", "medium", "high"]
        efforts = [effort for effort in efforts if effort in {
            "minimal", "low", "medium", "high", "max", "xhigh"}]
        default_effort = metadata.get("reasoning_effort_default")
        if re.fullmatch(r"gpt-5(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?", model_id):
            # The original GPT-5 family supports minimal through high in Chat Completions.
            efforts = ["minimal", "low", "medium", "high"]
            default_effort = "medium"
        values.update(reasoning_format="openai",
                      reasoning_efforts=efforts, thinking=True,
                      reasoning_effort=default_effort if default_effort in efforts else
                          ("high" if "high" in efforts else efforts[-1] if efforts else None),
                      supports_temperature=False)
    return ModelProfile(**values)
