"""DeepSeek chat-completion fields on the existing LangChain transport."""

from langchain_core.messages import AIMessage, AIMessageChunk
from app.llm.receipts import ObservedChatOpenAI, capture_response


def response_payload(response):
    payload = response if isinstance(response, dict) else response.model_dump()
    usage = payload.get("usage")
    if isinstance(usage, dict) and "prompt_cache_hit_tokens" in usage:
        details = usage.get("prompt_tokens_details") or {}
        if "cached_tokens" not in details:
            payload = {**payload, "usage": {
                **usage, "prompt_tokens_details": {
                    **details, "cached_tokens": usage["prompt_cache_hit_tokens"]},
            }}
    return payload


class DeepSeekChatModel(ObservedChatOpenAI):
    def validate_replay(self, messages):
        enabled = (self.extra_body or {}).get("thinking", {}).get("type") == "enabled"
        if enabled and any(isinstance(message, AIMessage)
                           and not isinstance(message.additional_kwargs.get("reasoning_content"), str)
                           for message in messages):
            raise ValueError("deepseek_reasoning_content_missing")

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        enabled = (self.extra_body or {}).get("thinking", {}).get("type") == "enabled"
        if enabled and payload.get("tools"):
            self.validate_replay(messages)
            choice = payload.get("tool_choice")
            if isinstance(choice, dict):
                tools = payload["tools"]
                name = choice.get("function", {}).get("name")
                if len(tools) != 1 or name and tools[0]["function"]["name"] != name:
                    raise ValueError("deepseek_thinking_forced_tool_unsupported")
            if choice == "required" or isinstance(choice, dict):
                payload["tool_choice"] = "auto"
            for original, outgoing in zip(messages, payload["messages"], strict=True):
                if isinstance(original, AIMessage):
                    reasoning = original.additional_kwargs.get("reasoning_content")
                    outgoing["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        capture_response(response)
        payload = response_payload(response)
        result = super()._create_chat_result(payload, generation_info)
        for generation, choice in zip(result.generations, payload["choices"], strict=True):
            reasoning = choice["message"].get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
            generation.message.response_metadata["model_provider"] = "deepseek"
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        payload = response_payload(chunk)
        result = super()._convert_chunk_to_generation_chunk(
            payload, default_chunk_class, base_generation_info)
        if result and isinstance(result.message, AIMessageChunk):
            result.message.response_metadata["model_provider"] = "deepseek"
            choices = payload.get("choices") or []
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
            if isinstance(reasoning, str):
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result
