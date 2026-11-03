"""Best-effort review telemetry; persistence and evidence remain authoritative."""

import asyncio
import json
import logging
import math
import os
import queue
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from uuid import uuid4

import requests
from langchain_core.tracers.context import tracing_v2_callback_var
from langsmith import Client, tracing_context
from langsmith import utils as langsmith_utils
from urllib3.util.retry import Retry

from app.core.config import settings

logger = logging.getLogger("Rivu.Observation")
_current = ContextVar("rivu_observation", default=None)
_exporter = None
_initialization_failed = False
_exporter_lock = threading.Lock()

# Only host-issued identifiers, counts and bounded decision codes cross this boundary.
_IDS = {"review_id", "job_id", "work_id", "attempt_id", "unit_id", "candidate_id", "ticket",
        "base_sha", "head_sha", "configuration_id"}
_COUNTS = {
    "job_attempt", "attempt", "model_calls", "tool_calls", "estimated_input_tokens",
    "input_tokens", "output_tokens", "cached_input_tokens", "unknown_calls",
    "remaining_input_tokens", "remaining_output_tokens", "remaining_seconds",
    "original_input_tokens", "projected_input_tokens", "saved_input_tokens", "epoch",
    "restored_sources", "investigation_questions", "source_count", "match_count",
    "path_count", "edge_count", "repeated_rounds", "candidate_count", "finding_count",
    "gap_count", "unit_count", "omitted_sources", "elapsed_ms", "call", "queue_ms",
    "shared_message_prefix_tokens", "minimum_reclaim_tokens", "retained_sources",
    "repeated_cycle_length", "repeated_request_count", "repeated_source_read_count",
}
_FLAGS = {"ok", "closing", "compacted", "pressure", "unresolved", "finding", "reused",
          "truncated", "partial", "scope_complete", "has_more", "usage_known", "capture_content"}
_LABELS = {
    "pressure_reason": {"capacity", "latency", "investigation"},
    "kind": {"review", "verification", "overview"},
    "status": {"completed", "partial", "failed", "interrupted", "cancelled", "ready",
               "skipped_empty", "skipped_deadline", "model", "fallback", "unverified"},
    "verdict": {"confirmed", "refuted", "insufficient"},
    "model_verdict": {"confirmed", "refuted", "insufficient"},
    "tool": {"read_file", "read_diff", "search_code", "find_files", "read_symbol", "find_relations",
             "read_discussion", "record_investigation", "recall_context", "submit_candidate"},
    "reason": {
        "hard_deadline", "review_cutoff", "tool_loop", "input_tokens", "output_tokens",
        "time", "call_fuse", "tool_limit", "format_correction", "scope_empty",
        "missing_decision_sources", "missing_confirmed_fields", "missing_change_attribution_sources",
        "invalid_source_refs", "invalid_cursor", "invalid_source_range", "invalid_source_side",
        "source_output_too_large", "source_range_out_of_bounds", "source_range_too_large",
        "source_too_large", "source_unavailable",
        "invalid_tool_arguments", "unknown_tool", "source_or_submission_rejected",
        "source_not_visible_in_this_attempt", "investigation_reference_not_received",
        "unsupported_glob", "invalid_path_filter", "invalid_exclude_globs", "invalid_page_size",
        "context_checkpoint_required", "tool_call_limit", "investigation_closed",
        "candidate_snapshot_mismatch", "candidate_location_not_sourced",
        "grouping_unavailable", "previous_grouping_request_not_replayed",
        "context_capacity", "input_token_limit", "output_token_limit", "model_call_limit",
        "agent_model_limit", "candidate_limit", "agent_tool_loop", "agent_structured_output_missing",
        "agent_execution_failed",
        "lease_lost", "review_stopped", "cancelled",
    },
}


def safe_fields(values):
    result = {}
    for key, value in values.items():
        if key in _IDS and isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
            result[key] = value
        elif key in _COUNTS and (value is None
                or type(value) is int and 0 <= value <= 2**63 - 1
                or type(value) is float and math.isfinite(value) and 0 <= value <= 2**63 - 1):
            result[key] = value
        elif key in _FLAGS and type(value) is bool:
            result[key] = value
        elif key in _LABELS and value is not None:
            result[key] = value if isinstance(value, str) and value in _LABELS[key] else "other"
    return result


def _content(value):
    """Opt-in payloads still redact configured credentials and common secret formats."""
    secrets = []
    truncated = False
    for key in ("openai_api_key", "github_token", "langsmith_api_key",
                "rivu_github_publish_token", "rivu_publication_api_token", "rivu_github_webhook_secret"):
        secret = getattr(settings, key, None)
        if hasattr(secret, "get_secret_value"):
            secret = secret.get_secret_value()
        if secret:
            secrets.append(str(secret))

    def clean(item, depth=0):
        nonlocal truncated
        if depth > 8:
            truncated = True
            return "[depth limit]"
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            item = re.sub(r"(?i)(?:Bearer\s+\S+|(?:sk-|ghp_|github_pat_)[\w-]{12,})",
                          "[redacted]", item)
            truncated = truncated or len(item) > 8192
            return item[:8192]
        if isinstance(item, dict):
            truncated = truncated or len(item) > 100
            return {str(key): "[redacted]" if re.search(
                r"(?i)password|secret|token|authorization|api.?key", str(key))
                else clean(val, depth + 1) for key, val in list(item.items())[:100]}
        if isinstance(item, (list, tuple)):
            truncated = truncated or len(item) > 100
            return [clean(val, depth + 1) for val in item[:100]]
        if item is None or type(item) in (int, float, bool):
            return item
        return "[unsupported]"

    cleaned = clean(value)
    if truncated:
        cleaned = ({**cleaned, "_rivu_truncated": True} if isinstance(cleaned, dict)
                   else {"value": cleaned, "_rivu_truncated": True})
    encoded = json.dumps(cleaned, ensure_ascii=True, allow_nan=False)
    return (json.loads(encoded) if len(encoded) <= 32768
            else {"preview": encoded[:32768], "truncated": True})


def export_failure(error):
    """Classify trusted exception types/statuses without formatting exception bodies."""
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 10:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    http_status = None
    for item in chain:
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            http_status = status
            break

    def has(*types):
        return any(isinstance(item, types) for item in chain)

    if has(requests.exceptions.SSLError):
        category = "tls"
    elif has(requests.exceptions.ProxyError):
        category = "proxy"
    elif has(requests.exceptions.Timeout, TimeoutError, langsmith_utils.LangSmithRequestTimeout):
        category = "timeout"
    elif has(requests.exceptions.ConnectionError, langsmith_utils.LangSmithConnectionError):
        category = "connection"
    elif http_status in {401, 403} or has(langsmith_utils.LangSmithAuthError):
        category = "authentication"
    elif http_status == 429 or has(langsmith_utils.LangSmithRateLimitError):
        category = "rate_limit"
    elif http_status in {400, 422}:
        category = "invalid_payload"
    elif http_status is not None:
        category = "http_error"
    elif has(ValueError, TypeError):
        category = "invalid_payload"
    elif has(langsmith_utils.LangSmithError):
        category = "sdk_error"
    else:
        category = "unexpected_error"
    return category, http_status


class Exporter:
    """One bounded queue; SDK/network work never runs on the review event loop."""

    def __init__(self):
        if any(os.getenv(key) is not None for key in (
                "LANGSMITH_USE_PYO3_CLIENT", "LANGCHAIN_USE_PYO3_CLIENT")):
            raise ValueError("native_tracing_queue_not_supported")
        self.client = Client(
            api_key=settings.langsmith_api_key,
            api_urls={settings.langsmith_endpoint or "https://api.smith.langchain.com":
                      settings.langsmith_api_key},
            auto_batch_tracing=False, timeout_ms=(
                round(settings.rivu_langsmith_connect_timeout_seconds * 1000),
                round(settings.rivu_langsmith_read_timeout_seconds * 1000)),
            retry_config=Retry(total=0),
            tracing_sampling_rate=1.0, omit_traced_runtime_info=True,
            tracing_mode="langsmith",
        )
        self.project = settings.langsmith_project
        self.pending = queue.Queue(maxsize=256)
        self.last_success = None
        self.failed = False
        self.dropped = 0
        self.closing_at = None
        self.thread = threading.Thread(target=self._run, name="rivu-observation", daemon=True)
        self.thread.start()

    def send(self, operation, payload):
        try:
            if self.closing_at is not None:
                self.dropped += 1
                return
            self.pending.put_nowait((operation, payload))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while self.closing_at is None or (
                not self.pending.empty() and monotonic() < self.closing_at):
            try:
                operation, payload = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if operation == "create":
                    self.client.create_run(project_name=self.project, **payload)
                else:
                    self.client.update_run(session_name=self.project, **payload)
                self.last_success = datetime.now(timezone.utc).isoformat()
                self.failed = False
            except Exception as exc:
                self.failed = True
                self.dropped += 1
                category, http_status = export_failure(exc)
                logger.warning(
                    "追踪上报失败 reason=export_failed operation=%s category=%s http_status=%s",
                    "create" if operation == "create" else "update", category, http_status)
            finally:
                self.pending.task_done()
        self.dropped += self.pending.qsize()
        try:
            self.client.close()
        except Exception:
            self.failed = True

    def close(self):
        self.closing_at = monotonic() + 2
        self.thread.join(timeout=2.1)


def _get_exporter():
    global _exporter, _initialization_failed
    if not settings.rivu_langsmith_tracing or not settings.langsmith_api_key:
        return None
    with _exporter_lock:
        if _exporter is None:
            try:
                _exporter = Exporter()
                _initialization_failed = False
            except Exception:
                _initialization_failed = True
                logger.warning("追踪初始化失败 reason=client_unavailable")
        return _exporter


def diagnostics():
    enabled = settings.rivu_langsmith_tracing
    status = ("disabled" if not enabled else "missing_credentials" if not settings.langsmith_api_key
              else "degraded" if _initialization_failed or (
                  _exporter and (_exporter.failed or _exporter.dropped))
              else "active" if _exporter and _exporter.last_success else "pending")
    return {
        "status": status,
        "capture_content": settings.rivu_langsmith_capture_content,
        "last_success_at": _exporter.last_success if _exporter and enabled else None,
        "dropped_operations": _exporter.dropped if _exporter and enabled else 0,
    }


async def close_observability():
    global _exporter, _initialization_failed
    exporter, _exporter = _exporter, None
    _initialization_failed = False
    if exporter:
        await asyncio.to_thread(exporter.close)


class Span:
    def __init__(self, name, fields, parent=None, exporter=None):
        self.id = uuid4()
        self.name = name
        self.fields = {**(parent.fields if parent else {}), **safe_fields(fields)}
        self.exporter = parent.exporter if parent else exporter
        self.capture_content = (parent.capture_content if parent else
                                settings.rivu_langsmith_capture_content)
        self.trace_id = parent.trace_id if parent else self.id
        self.parent_id = parent.id if parent else None
        self.usage_metadata = None
        self.payload = {}
        self.start = datetime.now(timezone.utc)
        self.started = monotonic()
        order = self.start.strftime("%Y%m%dT%H%M%S%fZ") + str(self.id)
        self.order = f"{parent.order}.{order}" if parent else order

    def send(self, operation, **payload):
        if self.exporter:
            try:
                self.exporter.send(operation, payload)
            except Exception:
                logger.warning("追踪入队失败 reason=enqueue_failed")

    def content(self, *, inputs=None, outputs=None):
        if not self.exporter or not self.capture_content:
            return
        try:
            payload = {}
            if inputs is not None:
                payload["inputs"] = _content(inputs)
            if outputs is not None:
                payload["outputs"] = _content(outputs)
            self.payload.update(payload)
        except Exception:
            logger.warning("追踪正文已省略 reason=content_unavailable")

    def set(self, **fields):
        self.fields.update(safe_fields(fields))

    def usage(self, usage):
        if not isinstance(usage, dict) or any(
                type(usage.get(key)) is not int or usage[key] < 0
                for key in ("input_tokens", "output_tokens")):
            self.set(usage_known=False)
            return
        self.usage_metadata = {
            "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        }
        details = usage.get("input_token_details")
        cached = details.get("cache_read") if isinstance(details, dict) else None
        if type(cached) is int and 0 <= cached <= usage["input_tokens"]:
            self.usage_metadata["input_token_details"] = {"cache_read": cached}
        self.set(usage_known=True, input_tokens=usage["input_tokens"],
                 output_tokens=usage["output_tokens"])


@contextmanager
def _span(span, run_type="chain"):
    token = _current.set(span)
    span.send("create", id=span.id, name=span.name, run_type=run_type,
              inputs={}, start_time=span.start, trace_id=span.trace_id,
              parent_run_id=span.parent_id, dotted_order=span.order,
              extra={"metadata": dict(span.fields)})
    error = None
    try:
        yield span
    except BaseException as exc:
        error = "cancelled" if isinstance(exc, asyncio.CancelledError) else "execution_stopped"
        raise
    finally:
        span.fields["elapsed_ms"] = round((monotonic() - span.started) * 1000, 2)
        metadata = dict(span.fields)
        if span.usage_metadata is not None:
            metadata["usage_metadata"] = span.usage_metadata
        span.send("update", run_id=span.id, name=span.name, run_type=run_type,
                  start_time=span.start, parent_run_id=span.parent_id,
                  end_time=datetime.now(timezone.utc), error=error,
                  extra={"metadata": metadata}, trace_id=span.trace_id, dotted_order=span.order,
                  **span.payload)
        logger.info("event=%s data=%s", span.name,
                    json.dumps({**span.fields, "trace_id": str(span.trace_id)}, sort_keys=True))
        _current.reset(token)


@contextmanager
def stage(name, *, run_type="chain", **fields):
    with _span(Span(name, fields, parent=_current.get()), run_type) as span:
        yield span


def event(name, **fields):
    with stage(name, **fields):
        pass


def settlement(ticket, receipt):
    usage = receipt["usage"]
    event("budget.settled", ticket=ticket, usage_known=receipt["state"] == "settled",
          reused=not receipt["changed"],
          input_tokens=usage["input_tokens"] if usage is not None else None,
          output_tokens=usage["output_tokens"] if usage is not None else None)


@contextmanager
def review_trace(run, lease, *, kind="review"):
    # Suppress ambient native tracers, which could upload unsanitized graph state.
    native = tracing_v2_callback_var.set(None)
    try:
        with tracing_context(enabled=False, parent=False, replicas=[], metadata={}, tags=[]):
            if _current.get() is not None:
                yield _current.get()
                return
            root = Span("review.execution", {
                "review_id": run.id, "job_id": lease["job_id"], "job_attempt": lease["attempt"],
                "kind": kind, "base_sha": run.snapshot.base_sha, "head_sha": run.snapshot.head_sha,
                "configuration_id": sha256(
                    run.configuration.model_dump_json().encode()).hexdigest(),
                "capture_content": settings.rivu_langsmith_capture_content,
            }, exporter=_get_exporter())
            with _span(root) as execution:
                yield execution
    finally:
        tracing_v2_callback_var.reset(native)


def message_content(messages):
    roles = {"human": "user", "ai": "assistant"}
    return [{"role": roles.get(message.type, message.type), "content": message.content,
             "tool_calls": getattr(message, "tool_calls", [])} for message in messages]
