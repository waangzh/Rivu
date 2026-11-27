"""Read old task profiles without accepting retired fields in new settings."""

from typing import Annotated, Any

from pydantic import BeforeValidator, RootModel, model_validator

from app.llm.profiles import ModelProfile


def _retired(profile):
    return (profile.get("api_protocol") in ("anthropic", "google", "openai-responses")
            or profile.get("reasoning_format") in ("anthropic", "google-budget", "google-level"))


class RetiredModelProfile(RootModel[dict[str, Any]]):
    @model_validator(mode="after")
    def require_retired_protocol(self):
        if not _retired(self.root):
            raise ValueError("profile_does_not_use_retired_protocol")
        return self


def _read_frozen_profile(value):
    if not isinstance(value, dict) or _retired(value):
        return value
    if value.get("api_protocol", "openai-chat") != "openai-chat":
        return value
    if "api_protocol" not in value and "reasoning_budget_tokens" not in value:
        return value
    profile = {key: item for key, item in value.items()
               if key not in {"api_protocol", "reasoning_budget_tokens"}}
    if profile.get("reasoning_format") == "deepseek":
        # The previous DeepSeek transport overrode both saved values.
        profile.update(max_tokens_parameter="max_tokens", stream_usage=True)
    return profile


FrozenModelProfile = Annotated[ModelProfile | RetiredModelProfile, BeforeValidator(_read_frozen_profile)]
