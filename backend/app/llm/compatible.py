"""Small OpenAI-compatible differences on LangChain's existing transport."""

from langchain_core.messages import AIMessage

from app.llm.receipts import ObservedChatOpenAI


class CompatibleChatModel(ObservedChatOpenAI):
    max_tokens_parameter: str = "max_tokens"
    replay_reasoning: bool = False
    model_provider_id: str = "openai-compatible"
    automatic_tool_choice: bool = False

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if self.max_tokens_parameter == "max_tokens" and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if self.automatic_tool_choice:
            choice = payload.get("tool_choice")
            if isinstance(choice, dict):
                tools = payload.get("tools") or []
                name = choice.get("function", {}).get("name")
                if len(tools) != 1 or name != tools[0]["function"]["name"]:
                    raise ValueError("thinking_forced_tool_unsupported")
            if choice in ("required", "any") or isinstance(choice, dict):
                payload["tool_choice"] = "auto"
        if self.replay_reasoning:
            for original, outgoing in zip(messages, payload["messages"], strict=True):
                if isinstance(original, AIMessage) and "reasoning_content" in original.additional_kwargs:
                    outgoing["reasoning_content"] = original.additional_kwargs["reasoning_content"]
        return payload

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        payload = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, payload["choices"], strict=True):
            reasoning = choice["message"].get("reasoning_content")
            if self.replay_reasoning and isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
            generation.message.response_metadata["model_provider"] = self.model_provider_id
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info)
        if result is not None and self.replay_reasoning:
            choices = chunk.get("choices") or []
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
            if isinstance(reasoning, str):
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result
