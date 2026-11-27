"""Declare the wire differences between Chat Completions endpoints."""

from dataclasses import dataclass

from app.llm.profiles import MaxTokensParameter, ModelProfile, ReasoningFormat


@dataclass(frozen=True)
class OpenAICompat:
    max_tokens_parameter: MaxTokensParameter
    thinking_format: ReasoningFormat | None
    replay_reasoning_content: bool
    strict_reasoning_replay: bool
    downgrade_forced_tool_choice: bool
    supports_temperature: bool
    stream_usage: bool

    @classmethod
    def from_profile(cls, profile: ModelProfile, thinking: bool):
        format_ = profile.reasoning_format
        return cls(
            max_tokens_parameter=profile.max_tokens_parameter,
            thinking_format=format_,
            replay_reasoning_content=thinking and format_ in {
                "deepseek", "glm", "kimi", "qwen", "chat-template",
            },
            strict_reasoning_replay=thinking and format_ == "deepseek",
            downgrade_forced_tool_choice=thinking and format_ in {"deepseek", "kimi"},
            supports_temperature=profile.supports_temperature and not (thinking and format_ == "deepseek"),
            stream_usage=profile.stream_usage,
        )
