"""Capture safe response facts before downstream message or agent parsing."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

from langchain_openai import ChatOpenAI

_CURRENT_RECEIPT = ContextVar("model_response_receipt", default=None)


@dataclass
class ResponseReceipt:
    received: bool = False
    parsed: bool = False
    usage: dict | None = None
    request_id: str | None = None
    finish_reason: str | None = None
    tool_calls: int = 0
    reasoning_present: bool = False


@contextmanager
def observe_response():
    receipt = ResponseReceipt()
    token = _CURRENT_RECEIPT.set(receipt)
    try:
        yield receipt
    finally:
        _CURRENT_RECEIPT.reset(token)


def safe_request_id(value):
    if (isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value)
            and not value.startswith(("sk-", "ghp_", "github_pat_"))):
        return value
    return None


def capture_response(response):
    receipt = _CURRENT_RECEIPT.get()
    if receipt is None:
        return
    receipt.received = True
    receipt.request_id = safe_request_id(getattr(response, "_request_id", None)) or receipt.request_id
    payload = response if isinstance(response, dict) else response.model_dump()
    usage = payload.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens)):
            receipt.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                             "total_tokens": input_tokens + output_tokens}
            details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
            cached = (details.get("cached_tokens") if isinstance(details, dict) else None)
            if cached is None:
                cached = usage.get("prompt_cache_hit_tokens")
            if type(cached) is int and 0 <= cached <= input_tokens:
                receipt.usage["input_token_details"] = {"cache_read": cached}
            details = usage.get("completion_tokens_details", usage.get("output_tokens_details"))
            reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
            if type(reasoning) is int and reasoning >= 0:
                receipt.usage["output_token_details"] = {"reasoning": reasoning}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason in {
                "stop", "length", "tool_calls", "content_filter", "function_call"}:
            receipt.finish_reason = reason
        message = choice.get("message") or choice.get("delta")
        if isinstance(message, dict):
            calls = message.get("tool_calls")
            receipt.tool_calls = len(calls) if isinstance(calls, list) else 0
            receipt.reasoning_present = receipt.reasoning_present or isinstance(
                message.get("reasoning_content"), str)
    output = payload.get("output")
    if isinstance(output, list):
        receipt.tool_calls = sum(item.get("type") == "function_call" for item in output)
        receipt.reasoning_present = any(item.get("type") == "reasoning" for item in output)
        receipt.finish_reason = {"completed": "stop", "incomplete": "length"}.get(payload.get("status"))


async def capture_http_response(response):
    if response.is_success and "application/json" in response.headers.get("content-type", ""):
        await response.aread()
        capture_response(response.json())
        receipt = _CURRENT_RECEIPT.get()
        if receipt is not None:
            receipt.request_id = safe_request_id(response.headers.get("x-request-id"))


def capture_native_response(response, protocol):
    payload = response.model_dump()
    usage = payload.get("usage" if protocol == "anthropic" else "usage_metadata")
    normalized = None
    if isinstance(usage, dict):
        input_key, output_key = (("input_tokens", "output_tokens") if protocol == "anthropic"
                                else ("prompt_token_count", "candidates_token_count"))
        input_tokens, output_tokens = usage.get(input_key), usage.get(output_key)
        if all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens)):
            cached = usage.get("cache_read_input_tokens" if protocol == "anthropic"
                               else "cached_content_token_count") or 0
            creation = usage.get("cache_creation_input_tokens") or 0
            if protocol == "anthropic":
                input_tokens += cached + creation
            else:
                output_tokens += usage.get("thoughts_token_count") or 0
                input_tokens += usage.get("tool_use_prompt_token_count") or 0
            normalized = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                          "total_tokens": input_tokens + output_tokens,
                          "input_token_details": {"cache_read": cached, "cache_creation": creation}}
            reasoning = (usage.get("output_tokens_details") or {}).get("thinking_tokens") \
                if protocol == "anthropic" else usage.get("thoughts_token_count")
            if type(reasoning) is int and reasoning >= 0:
                normalized["output_token_details"] = {"reasoning": reasoning}
    receipt = _CURRENT_RECEIPT.get()
    if receipt is not None:
        receipt.received = True
        receipt.usage = normalized
        receipt.request_id = safe_request_id(getattr(response, "_request_id", None))
    return normalized


def capture_result(result, usage):
    for generation in result.generations:
        generation.message.usage_metadata = usage
    receipt = _CURRENT_RECEIPT.get()
    if receipt is not None:
        receipt.parsed = True
        message = result.generations[0].message
        receipt.tool_calls = len(message.tool_calls)
        if isinstance(message.content, list):
            receipt.reasoning_present = any(isinstance(block, dict) and
                block.get("type") in {"reasoning", "thinking"} for block in message.content)


class ObservedChatOpenAI(ChatOpenAI):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if self.use_responses_api:
            receipt = _CURRENT_RECEIPT.get()
            if receipt is not None:
                capture_result(result, receipt.usage)
        return result

    def _create_chat_result(self, response, generation_info=None):
        capture_response(response)
        result = super()._create_chat_result(response, generation_info)
        payload = response if isinstance(response, dict) else response.model_dump()
        usage = payload.get("usage")
        if not (isinstance(usage, dict) and all(
                type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens"))):
            for generation in result.generations:
                generation.message.usage_metadata = None
        receipt = _CURRENT_RECEIPT.get()
        if receipt is not None:
            receipt.parsed = True
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        capture_response(chunk)
        if (chunk.get("usage") and chunk.get("choices")
                and all(choice.get("delta") is None for choice in chunk["choices"])):
            chunk = {**chunk, "choices": []}
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info)
        usage = chunk.get("usage")
        if result is not None and usage is not None and not (
                isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens"))):
            result.message.usage_metadata = None
        receipt = _CURRENT_RECEIPT.get()
        if receipt is not None and result is not None:
            receipt.parsed = True
        return result
