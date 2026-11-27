"""Capture safe response facts before downstream message or agent parsing."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

from langchain_core.messages.ai import UsageMetadata

_CURRENT_RECEIPT = ContextVar("model_response_receipt", default=None)


@dataclass
class ResponseReceipt:
    received: bool = False
    parsed: bool = False
    usage: UsageMetadata | None = None
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


def _normalize_usage(usage) -> UsageMetadata | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if not all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens)):
        return None
    normalized = UsageMetadata(input_tokens=input_tokens, output_tokens=output_tokens,
                               total_tokens=input_tokens + output_tokens)
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    if type(cached) is int and 0 <= cached <= input_tokens:
        normalized["input_token_details"] = {"cache_read": cached}
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(reasoning) is int and reasoning >= 0:
        normalized["output_token_details"] = {"reasoning": reasoning}
    return normalized


def capture_response(payload, *, request_id=None):
    usage = _normalize_usage(payload.get("usage"))
    receipt = _CURRENT_RECEIPT.get()
    if receipt is None:
        return usage
    receipt.received = True
    receipt.request_id = safe_request_id(request_id) or receipt.request_id
    if usage is not None:
        receipt.usage = usage
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
            if isinstance(calls, list):
                receipt.tool_calls = len(calls)
            receipt.reasoning_present = receipt.reasoning_present or isinstance(
                message.get("reasoning_content"), str)
    return usage


def mark_response_parsed():
    receipt = _CURRENT_RECEIPT.get()
    if receipt is not None:
        receipt.parsed = True
