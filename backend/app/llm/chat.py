"""One Chat Completions transport for all registered endpoints."""

from typing import Literal

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

from app.llm.compat import OpenAICompat
from app.llm.receipts import capture_response, mark_response_parsed

REASONING_CONTENT_MISSING = "reasoning_content_missing"
FORCED_TOOL_UNSUPPORTED = "thinking_forced_tool_unsupported"
_REQUEST_PREPARATION_ERRORS = frozenset({REASONING_CONTENT_MISSING, FORCED_TOOL_UNSUPPORTED})


def request_preparation_error(exc):
    if type(exc) is ValueError and str(exc) in _REQUEST_PREPARATION_ERRORS:
        return str(exc)
    return None


def validate_model_replay(model, messages):
    """Preflight the history for a request that binds tools."""
    validate = getattr(model, "validate_replay", None)
    if validate is not None:
        validate(messages)


def thinking_options(format_, enabled, effort):
    state = {"type": "enabled" if enabled else "disabled"}
    match format_:
        case "openai":
            return {"reasoning_effort": effort or "high"}
        case "deepseek":
            options = {"extra_body": {"thinking": state}}
            if enabled:
                options["reasoning_effort"] = effort or "high"
            return options
        case "glm" | "kimi":
            return {"extra_body": {"thinking": state}}
        case "qwen":
            return {"extra_body": {"enable_thinking": enabled}}
        case "chat-template":
            return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}
    return {}


class OpenAIChatModel(ChatOpenAI):
    compat: OpenAICompat
    model_provider_id: str
    use_responses_api: Literal[False] = False

    def validate_replay(self, messages):
        if self.compat.strict_reasoning_replay and any(
                isinstance(message, AIMessage)
                and not isinstance(message.additional_kwargs.get("reasoning_content"), str)
                for message in messages):
            raise ValueError(REASONING_CONTENT_MISSING)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if self.compat.max_tokens_parameter == "max_tokens" and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        tools = payload.get("tools")
        if tools:
            self.validate_replay(messages)
        if self.compat.downgrade_forced_tool_choice:
            choice = payload.get("tool_choice")
            if isinstance(choice, dict):
                name = choice.get("function", {}).get("name")
                if not name or not tools or len(tools) != 1 or name != tools[0].get("function", {}).get("name"):
                    raise ValueError(FORCED_TOOL_UNSUPPORTED)
            if choice in ("required", "any") or isinstance(choice, dict):
                payload["tool_choice"] = "auto"
        if self.compat.replay_reasoning_content and (tools or not self.compat.strict_reasoning_replay):
            for original, outgoing in zip(messages, payload["messages"], strict=True):
                if isinstance(original, AIMessage):
                    reasoning = original.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str):
                        outgoing["reasoning_content"] = reasoning
        return payload

    def _capture_reasoning(self, message, wire_message):
        message.response_metadata["model_provider"] = self.model_provider_id
        if self.compat.replay_reasoning_content or self.compat.thinking_format == "deepseek":
            reasoning = wire_message.get("reasoning_content")
            if isinstance(reasoning, str):
                message.additional_kwargs["reasoning_content"] = reasoning

    def _create_chat_result(self, response, generation_info=None):
        payload = response if isinstance(response, dict) else response.model_dump(warnings=False)
        usage = capture_response(payload, request_id=getattr(response, "_request_id", None))
        # Set normalized usage ourselves so the SDK cannot fill missing counters with zeros.
        if payload.get("usage") is not None:
            response = ({**response, "usage": None} if isinstance(response, dict)
                        else response.model_copy(update={"usage": None}))
        result = super()._create_chat_result(response, generation_info)
        for generation, choice in zip(result.generations, payload["choices"], strict=True):
            if isinstance(generation.message, AIMessage):
                generation.message.usage_metadata = usage
                self._capture_reasoning(generation.message, choice["message"])
        mark_response_parsed()
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        usage = capture_response(chunk)
        choices = chunk.get("choices") or []
        if chunk.get("usage") is not None:
            chunk = {**chunk, "usage": None}
            if all(choice.get("delta") is None for choice in choices):
                chunk["choices"] = []
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info)
        if result is not None:
            if isinstance(result.message, AIMessageChunk):
                result.message.usage_metadata = usage
                self._capture_reasoning(result.message, (choices[0].get("delta") or {}) if choices else {})
            mark_response_parsed()
        return result
