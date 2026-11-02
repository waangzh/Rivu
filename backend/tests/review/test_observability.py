import asyncio
import json
import logging
import sys
import threading
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi import FastAPI
from langchain_core.tracers.context import tracing_v2_callback_var
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langsmith import tracing_context
from langsmith.run_helpers import get_tracing_context

from app.core.config import settings
from app.review import observability as obs
from app.review.agent import AgentRunner
from app.review.orchestrator import ReviewOrchestrator
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_run, setup_source_run


class RecordingExporter:
    def __init__(self):
        self.operations = []

    def send(self, operation, payload):
        self.operations.append((operation, deepcopy(payload)))


def test_default_test_environment_does_not_enable_tracing():
    assert settings.rivu_langsmith_tracing is False
    assert settings.rivu_langsmith_capture_content is False
    assert get_tracing_context()["enabled"] is False


@pytest.fixture
def recorder(monkeypatch):
    exporter = RecordingExporter()
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "fixture-tracing-key")
    monkeypatch.setattr(settings, "rivu_langsmith_capture_content", False)
    monkeypatch.setattr(obs, "_get_exporter", lambda: exporter)
    return exporter


async def test_complete_review_has_one_tree_and_metadata_without_source(tmp_path, recorder, caplog):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    with caplog.at_level(logging.INFO, logger="Rivu.Observation"):
        report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    creates = [payload for operation, payload in recorder.operations if operation == "create"]
    roots = [item for item in creates if item["parent_run_id"] is None]
    assert len(roots) == 1
    root = roots[0]
    by_id = {item["id"]: item for item in creates}
    assert root["name"] == "review.execution"
    assert all(item["trace_id"] == root["id"] for item in creates)
    for item in creates[1:]:
        parent = by_id[item["parent_run_id"]]
        assert item["dotted_order"].startswith(parent["dotted_order"] + ".")
    names = [item["name"] for item in creates]
    assert names.count("model.request") == report.usage["model_calls"] == 4
    assert names.count("agent.attempt") == 2
    assert {"tool.execute", "candidate.submitted", "candidate.verified", "context.projected",
            "budget.reserved", "budget.settled", "review.report", "review.overview"} <= set(names)
    verifications = [payload["extra"]["metadata"] for operation, payload in recorder.operations
                     if operation == "create" and payload["name"] == "candidate.verified"]
    assert verifications[0]["model_verdict"] == verifications[0]["verdict"] == "confirmed"
    assert verifications[0]["finding"] is True
    for operation, payload in recorder.operations:
        if operation == "create":
            assert payload["inputs"] == {}
        assert "outputs" not in payload
        assert "serialized" not in payload
    encoded = json.dumps(recorder.operations, default=str)
    model_usage = [payload["extra"]["metadata"]["usage_metadata"]
                   for operation, payload in recorder.operations
                   if operation == "update" and payload.get("extra", {}).get("metadata", {}).get(
                       "usage_metadata") is not None]
    assert len(model_usage) == 4
    assert sum(item["input_tokens"] for item in model_usage) == report.usage["input_tokens"]
    for secret in ("def ratio", "return 10", "a.py", "零输入导致除零", "fixture-tracing-key",
                   "https://github.com", "http://localhost"):
        assert secret not in encoded
        assert secret not in caplog.text
    assert obs._current.get() is None


async def test_disabled_overrides_environment_and_explicit_ambient_native_tracer(
        tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_USE_PYO3_CLIENT", "1")
    monkeypatch.setattr(obs, "Exporter", lambda: pytest.fail("disabled tracing created a client"))
    store, run, lease, provider, _ = setup_source_run(tmp_path)

    class InspectModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            assert get_tracing_context()["enabled"] is False
            assert tracing_v2_callback_var.get() is None
            assert not any(type(handler).__name__ == "LangChainTracer"
                           for handler in run_manager.handlers)
            return super()._generate(messages, stop, run_manager, **kwargs)

    ambient = object()
    token = tracing_v2_callback_var.set(ambient)
    try:
        with tracing_context(enabled=True, metadata={"secret": "ambient-value"}):
            report = await ReviewOrchestrator(store, AgentRunner(InspectModel())).run(
                run.id, provider, lease)
            assert get_tracing_context()["enabled"] is True
            assert tracing_v2_callback_var.get() is ambient
        assert report.status == "completed"
    finally:
        tracing_v2_callback_var.reset(token)


async def test_opt_in_content_redacts_credentials_and_never_puts_content_in_logs(
        tmp_path, recorder, monkeypatch, caplog):
    monkeypatch.setattr(settings, "rivu_langsmith_capture_content", True)
    monkeypatch.setattr(settings, "openai_api_key", "fixture-provider-secret")
    store, run, lease, provider, _ = setup_source_run(
        tmp_path, head_source="def ratio(x):\n    return 10 / x  # fixture-provider-secret\n")
    with caplog.at_level(logging.INFO, logger="Rivu.Observation"):
        await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    encoded = json.dumps(recorder.operations, default=str)
    assert "def ratio" in encoded
    assert "[redacted]" in encoded
    assert "fixture-provider-secret" not in encoded
    assert "def ratio" not in caplog.text
    assert "fixture-provider-secret" not in caplog.text


async def test_export_enqueue_failure_never_changes_review_or_usage(tmp_path, monkeypatch, caplog):
    class FailingExporter:
        def send(self, *_args):
            raise RuntimeError("provider-error-secret")

    monkeypatch.setattr(obs, "_get_exporter", FailingExporter)
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel(unknown_usage=True))).run(
        run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    assert report.usage["model_calls"] == report.usage["unknown_calls"] == 4
    assert report.usage["input_tokens"] is None
    assert report.usage["committed_input_tokens"] > 0
    assert "provider-error-secret" not in caplog.text
    assert "enqueue_failed" in caplog.text


async def test_model_confirmation_is_recorded_after_durable_admission_as_insufficient(
        tmp_path, recorder):
    class UnsourcedVerifier(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            message = result.generations[0].message
            if message.tool_calls[0]["name"] == "VerificationOutput":
                message.tool_calls[0]["args"]["source_refs"] = []
            return result

    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(UnsourcedVerifier())).run(
        run.id, provider, lease)
    assert report.findings == []
    assert store.candidates(run.id)[0].verification.verdict == "insufficient"
    event = next(payload["extra"]["metadata"] for operation, payload in recorder.operations
                 if operation == "create" and payload["name"] == "candidate.verified")
    assert event["model_verdict"] == "confirmed"
    assert event["verdict"] == "insufficient"
    assert event["reason"] == "missing_decision_sources"
    assert event["finding"] is False
    assert report.usage["model_calls"] == 5


async def test_duplicate_settlement_observes_ledger_instead_of_ignored_usage(tmp_path, recorder):
    from app.review.budget import BudgetGate

    store, run, lease, _ = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    budget = BudgetGate(store, run, lease)
    with obs.review_trace(run, lease):
        known = await budget.reserve(work, 1000)
        await budget.settle(known, {"input_tokens": 100, "output_tokens": 5})
        await budget.settle(known, None)
        unknown = await budget.reserve(work, 1000)
        await budget.settle(unknown, None)
        await budget.settle(unknown, {"input_tokens": 0, "output_tokens": 0})
    events = [payload["extra"]["metadata"] for operation, payload in recorder.operations
              if operation == "create" and payload["name"] == "budget.settled"]
    assert [item["reused"] for item in events] == [False, True, False, True]
    assert [item["usage_known"] for item in events] == [True, True, False, False]
    assert [item["input_tokens"] for item in events] == [100, 100, None, None]
    assert store.usage(run.id)["unknown_calls"] == 1


async def test_paginated_tool_is_observed_as_partial_without_logging_query(tmp_path, recorder):
    class SearchModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if (not str(messages[0].content).startswith("Summarize")
                    and not any(isinstance(message, ToolMessage) for message in messages)):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="", tool_calls=[{"name": "search_code", "id": "search",
                                            "args": {"query": "return", "limit": 1}}],
                    usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}))])
            return super()._generate(messages, stop, run_manager, **kwargs)

    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "consumer.py": "def consume():\n    return 42\n",
    })
    await ReviewOrchestrator(store, AgentRunner(SearchModel())).run(run.id, provider, lease)
    tools = [payload["extra"]["metadata"] for operation, payload in recorder.operations
             if operation == "update" and payload.get("extra", {}).get(
                 "metadata", {}).get("tool") == "search_code"]
    assert tools
    assert tools[0]["partial"] is True
    assert tools[0]["scope_complete"] is False
    assert tools[0]["has_more"] is True
    assert tools[0]["match_count"] == 1
    assert "query" not in json.dumps(recorder.operations, default=str)


async def test_parallel_agents_and_cancelled_spans_keep_context_isolated(
        tmp_path, recorder):
    _, run, lease, _, _ = setup_source_run(tmp_path)

    async def agent(work_id, cancelled=False):
        with obs.stage("agent.attempt", work_id=work_id):
            await asyncio.sleep(0)
            if cancelled:
                raise asyncio.CancelledError
            obs.event("candidate.submitted", candidate_id=work_id)

    with obs.review_trace(run, lease):
        result = await asyncio.gather(agent("first"), agent("second", True), return_exceptions=True)
        obs.event("review.finished")
    assert isinstance(result[1], asyncio.CancelledError)
    creates = {payload["id"]: payload for operation, payload in recorder.operations
               if operation == "create"}
    submitted = next(payload for payload in creates.values()
                     if payload["name"] == "candidate.submitted")
    assert creates[submitted["parent_run_id"]]["extra"]["metadata"]["work_id"] == "first"
    errors = [payload for operation, payload in recorder.operations
              if operation == "update" and payload.get("error")]
    assert len(errors) == 1 and errors[0]["error"] == "cancelled"
    assert obs._current.get() is None


def test_fields_reject_unknown_metadata_and_content_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "private-value")
    assert obs.safe_fields({
        "reason": "source text private-value", "input_tokens": None, "output_tokens": -2,
        "head_sha": "path/to/private/file", "source": "private-value", "ok": True,
    }) == {"reason": "other", "input_tokens": None, "ok": True}
    encoded = json.dumps(obs._content({"api_key": "private-value", "text": "private-value",
                                      "items": ["x" * 9000] * 100}))
    assert "private-value" not in encoded
    assert len(encoded) < 40000
    assert obs.safe_fields({"input_tokens": 10**1000, "output_tokens": float("inf")}) == {}
    assert obs._content({"text": "x" * 9000})["_rivu_truncated"] is True


async def test_diagnostics_reports_observed_state_without_credentials(tmp_path, monkeypatch):
    from app.api import system

    monkeypatch.setattr(obs, "_exporter", None)
    monkeypatch.setattr(obs, "_initialization_failed", False)
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", None)
    monkeypatch.setattr(system, "get_review_service", lambda: SimpleNamespace(worker_running=lambda: False))
    monkeypatch.setattr(system, "schema_is_current", lambda: True)
    monkeypatch.setattr(settings, "rivu_artifact_dir", tmp_path)
    app = FastAPI()
    app.include_router(system.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/system/diagnostics")
    assert response.status_code == 200
    assert response.json()["langsmith"]["status"] == "missing_credentials"
    monkeypatch.setattr(settings, "langsmith_api_key", "never-return-this")
    assert obs.diagnostics()["status"] == "pending"
    monkeypatch.setattr(obs, "_exporter", SimpleNamespace(
        failed=False, dropped=0, last_success="2026-11-01T00:00:00+00:00"))
    assert obs.diagnostics()["status"] == "active"
    obs._exporter.dropped = 1
    assert obs.diagnostics()["status"] == "degraded"
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", False)
    assert obs.diagnostics() == {
        "status": "disabled", "capture_content": settings.rivu_langsmith_capture_content,
        "last_success_at": None, "dropped_operations": 0,
    }


async def test_exporter_queue_is_bounded_and_network_failure_stays_in_background(
        monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    thread_ids = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["auto_batch_tracing"] is False
            assert kwargs["tracing_mode"] == "langsmith"
            assert len(kwargs["api_urls"]) == 1

        def create_run(self, **_kwargs):
            thread_ids.append(threading.get_ident())
            entered.set()
            release.wait(timeout=2)
            raise RuntimeError("network-response-secret")

        def close(self):
            pass

    monkeypatch.setattr(obs, "Client", Client)
    exporter = obs.Exporter()
    exporter.send("create", {"name": "review.execution"})
    assert await asyncio.to_thread(entered.wait, 1)
    for _ in range(300):
        exporter.send("create", {"name": "review.execution"})
    assert exporter.pending.qsize() == 256
    assert exporter.dropped == 44
    release.set()
    await asyncio.to_thread(exporter.close)
    assert thread_ids and all(value != threading.get_ident() for value in thread_ids)
    assert exporter.failed
    assert "network-response-secret" not in caplog.text


async def test_real_sdk_payloads_use_only_configured_destination_and_safe_metadata(
        tmp_path, monkeypatch, caplog):
    from langsmith import Client

    requests_seen = []
    created = {}
    finished = {}

    class Session(requests.Session):
        def send(self, request, **kwargs):
            requests_seen.append(request)
            body = json.loads(request.body)
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            response.request = request
            if request.method == "POST":
                created[body["id"]] = body
            else:
                original = created[body["id"]]
                if any(body.get(key) != original.get(key)
                       for key in ("parent_run_id", "trace_id", "dotted_order")):
                    response.status_code = 400
                    response._content = b'{"detail":"private-response-body"}'
                else:
                    finished[body["id"]] = body
            return response

    def client(**kwargs):
        return Client(session=Session(), **kwargs)

    monkeypatch.setattr(obs, "Client", client)
    monkeypatch.setattr(settings, "langsmith_endpoint", "https://trace.example.test")
    monkeypatch.setattr(settings, "langsmith_api_key", "trace-key")
    monkeypatch.setattr(settings, "langsmith_project", "fixture-project")
    monkeypatch.setenv("LANGSMITH_RUNS_ENDPOINTS", '{"https://unexpected.example.test":"other-key"}')
    monkeypatch.setenv("LANGSMITH_TRACING_MODE", "otel")
    monkeypatch.setenv("LANGCHAIN_PRIVATE_VALUE", "do-not-export")
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    monkeypatch.delenv("LANGCHAIN_ENDPOINT", raising=False)
    _, run, lease, _, _ = setup_source_run(tmp_path)
    exporter = obs.Exporter()
    monkeypatch.setattr(obs, "_get_exporter", lambda: exporter)
    with obs.review_trace(run, lease):
        with obs.stage("model.request", run_type="llm") as span:
            span.usage({"input_tokens": 10, "output_tokens": 3,
                        "input_token_details": {"cache_read": 6, "private": "do-not-export"}})
    await asyncio.to_thread(exporter.close)
    assert not exporter.failed
    assert exporter.dropped == 0
    assert len(requests_seen) == 4
    assert set(finished) == set(created)
    for run_id, start in created.items():
        end = finished[run_id]
        assert end["end_time"]
        assert end["name"] == start["name"]
        assert end["run_type"] == start["run_type"]
        assert end["session_name"] == start["session_name"]
    assert all(request.url.startswith("https://trace.example.test/runs")
               for request in requests_seen)
    bodies = [json.loads(request.body) for request in requests_seen]
    assert bodies[0]["session_name"] == "fixture-project"
    llm_end = next(body for body in bodies if
                   body.get("extra", {}).get("metadata", {}).get("usage_metadata"))
    assert llm_end["extra"]["metadata"]["usage_metadata"] == {
        "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
        "input_token_details": {"cache_read": 6},
    }
    encoded = json.dumps(bodies)
    assert "do-not-export" not in encoded
    assert "trace-key" not in encoded
    assert "unexpected.example.test" not in encoded
    assert "private-response-body" not in caplog.text


@pytest.mark.parametrize(("status", "category"), [
    (400, "invalid_payload"), (401, "authentication"), (403, "authentication"),
    (422, "invalid_payload"), (429, "rate_limit"), (503, "http_error"),
])
async def test_real_sdk_rejections_log_operation_and_status_without_private_body(
        monkeypatch, caplog, status, category):
    from langsmith import Client

    sent = []

    class Session(requests.Session):
        def send(self, request, **kwargs):
            sent.append(request.method)
            response = requests.Response()
            response.status_code = status
            response._content = b'{"detail":"private-response-body trace-secret"}'
            response.request = request
            return response

    monkeypatch.setattr(settings, "langsmith_api_key", "trace-secret")
    monkeypatch.setattr(settings, "langsmith_endpoint", "https://trace.example.test")
    monkeypatch.setattr(obs, "Client", lambda **kwargs: Client(session=Session(), **kwargs))
    exporter = obs.Exporter()
    with obs._span(obs.Span("review.execution", {}, exporter=exporter)):
        pass
    await asyncio.to_thread(exporter.close)
    assert sent == ["POST", "PATCH"]
    assert exporter.dropped == 2
    assert exporter.failed
    assert f"category={category}" in caplog.text
    assert f"http_status={status}" in caplog.text
    assert "operation=create" in caplog.text
    assert "operation=update" in caplog.text
    for private in ("private-response-body", "trace-secret", "trace.example.test"):
        assert private not in caplog.text


@pytest.mark.parametrize(("exception", "category"), [
    (requests.exceptions.ConnectTimeout, "timeout"),
    (requests.exceptions.ReadTimeout, "timeout"),
    (requests.exceptions.SSLError, "tls"),
    (requests.exceptions.ProxyError, "proxy"),
    (requests.exceptions.ConnectionError, "connection"),
])
async def test_real_sdk_network_errors_are_classified_without_exception_text(
        monkeypatch, caplog, exception, category):
    from langsmith import Client

    sent = []

    class Session(requests.Session):
        def send(self, request, **kwargs):
            sent.append(request.method)
            raise exception("private-network-error", request=request)

    monkeypatch.setattr(settings, "langsmith_api_key", "trace-secret")
    monkeypatch.setattr(settings, "langsmith_endpoint", "https://trace.example.test")
    monkeypatch.setattr(obs, "Client", lambda **kwargs: Client(session=Session(), **kwargs))
    exporter = obs.Exporter()
    with obs._span(obs.Span("review.execution", {}, exporter=exporter)):
        pass
    await asyncio.to_thread(exporter.close)
    assert sent == ["POST", "PATCH"]
    assert exporter.dropped == 2
    assert f"category={category}" in caplog.text
    assert "http_status=None" in caplog.text
    for private in ("private-network-error", "trace-secret", "trace.example.test"):
        assert private not in caplog.text


async def test_export_timeouts_are_configurable_and_retries_remain_disabled(monkeypatch):
    configured = {}

    class Client:
        def __init__(self, **kwargs):
            configured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(obs, "Client", Client)
    monkeypatch.setattr(settings, "rivu_langsmith_connect_timeout_seconds", 3.25)
    monkeypatch.setattr(settings, "rivu_langsmith_read_timeout_seconds", 7.5)
    exporter = obs.Exporter()
    await asyncio.to_thread(exporter.close)
    assert configured["timeout_ms"] == (3250, 7500)
    assert configured["retry_config"].total == 0
    assert configured["auto_batch_tracing"] is False


def test_export_timeout_configuration_defaults_and_bounds(monkeypatch):
    from app.core.config import Settings
    from pydantic import ValidationError

    fields = ("rivu_langsmith_connect_timeout_seconds", "rivu_langsmith_read_timeout_seconds")
    for name in fields:
        monkeypatch.delenv(name.upper(), raising=False)
    config = Settings(_env_file=None)
    assert config.rivu_langsmith_connect_timeout_seconds == 3
    assert config.rivu_langsmith_read_timeout_seconds == 10
    for name in fields:
        assert getattr(Settings(_env_file=None, **{name: 0.1}), name) == 0.1
        assert getattr(Settings(_env_file=None, **{name: 60}), name) == 60
        for value in (0, -1, 60.1, float("inf"), float("nan")):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, **{name: value})


@pytest.mark.parametrize("key", ["LANGSMITH_USE_PYO3_CLIENT", "LANGCHAIN_USE_PYO3_CLIENT"])
async def test_native_sdk_queue_configuration_disables_export_without_breaking_review(
        tmp_path, monkeypatch, key):
    def forbidden(*_args, **_kwargs):
        pytest.fail("native tracing client must not be constructed")

    monkeypatch.setenv(key, "false")  # The SDK treats presence, not truthiness, as enabling it.
    monkeypatch.setitem(sys.modules, "langsmith_pyo3", SimpleNamespace(BlockingTracingClient=forbidden))
    monkeypatch.setattr(obs, "Client", forbidden)
    monkeypatch.setattr(obs, "_exporter", None)
    monkeypatch.setattr(obs, "_initialization_failed", False)
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "fixture-key")
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "completed"
    assert len(report.findings) == 1
    assert obs._exporter is None
    assert obs.diagnostics()["status"] == "degraded"
