import json
from datetime import timedelta

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import select

from app.llm.providers import OpenAICompatibleProvider
from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.domain import utcnow
from app.review.orchestrator import ReviewOrchestrator
from app.review.planner import tokens
from app.review.store import OVERVIEW_INPUT
from tests.review.vnext_helpers import completion_gap, setup_source_run


@pytest.mark.parametrize(("line_count", "minimum_input"), [(150, 32_000), (650, 200_000)])
async def test_reviewer_and_verifier_receive_initial_source_packets_beyond_grouping_limits(
    tmp_path, line_count, minimum_input,
):
    def large_source(value):
        return "".join(f"VALUES_{index} = [{value}," + "0," * 75 + "]\n" for index in range(line_count))

    store, run, lease, provider, sessions = setup_source_run(
        tmp_path, base_source=large_source(0), head_source=large_source(1))
    run.configuration.options.limits.total_input_tokens = None
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []
    initial_sizes = {}

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        extra_calls = []
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整固定列表值。", "source_refs": ["S1"]}],
            }
        else:
            role = "reviewer" if "UnitCompletion" in names else "verifier"
            initial_sizes[role] = tokens({"messages": payload["messages"], "tools": payload["tools"]})
            assert initial_sizes[role] > minimum_input
            assert initial_sizes[role] + run.configuration.options.limits.output_tokens \
                < run.configuration.options.limits.context_tokens
            assert not packet["missing_material"]
            assert {material["member"]["id"] for material in packet["materials"]} == set(
                run.plan.units[0].required_hunk_ids)
            if role == "reviewer":
                refs = [source["source_ref"] for material in packet["materials"]
                        for source in material["sources"]]
                name, result = "submit_candidate", {"proposal": {
                    "claim": "固定源码候选夹具", "trigger": "指定输入", "impact": "结果改变",
                    "source_refs": refs,
                }}
                extra_calls = [{"id": "done", "type": "function", "function": {
                    "name": "UnitCompletion", "arguments": "{}"}}]
            else:
                assert not packet["missing_candidate_materials"]
                name, result = "VerificationOutput", {
                    "verdict": "refuted", "attribution": "uncertain",
                    "reason": "固定源码仅修改列表值，未发生候选所称的操作失败。",
                    "source_refs": [source["source_ref"] for source in packet["candidate_materials"]],
                }
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)},
                }, *extra_calls],
            }}],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 5, "total_tokens": 100_005},
        })

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1",
                          http_client=client, max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert set(initial_sizes) == {"reviewer", "verifier"}
    assert report.status == "completed"
    assert len(requests) == report.usage["model_calls"] == 3
    assert report.candidates[0].verification.verdict == "refuted"
    assert not report.findings


async def test_real_provider_adapter_uses_tools_and_all_calls_have_tickets(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "response_format" not in payload
        assert "runtime" not in json.dumps(payload["tools"])
        system = payload["messages"][0]["content"]
        packet = json.loads(payload["messages"][1]["content"])
        if system.startswith("Summarize"):
            name = "OverviewOutput"
            result = {"change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
                      "finding_order": []}
        else:
            name, result = "UnitCompletion", {}
            assert packet["materials"]
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "final", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                      "prompt_tokens_details": {"cached_tokens": 6}}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == "completed"
    assert report.overview.status == "ready"
    assert len(requests) == report.usage["model_calls"] == 2
    assert report.usage["input_tokens"] == 20
    assert report.usage["cached_input_tokens"] == 12
    assert report.usage["uncached_input_tokens"] == 8
    assert report.usage["cache_hit_rate"] == 0.6


async def test_short_investigation_notes_preserve_cache_prefix_and_unresolved_result(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "consumer.py": "def consume():\n    return 42\n",
    })
    reviews = []
    handle = None
    notes = {
        "question_id": "consumer-contract", "question": "Does the consumer constrain the input?",
        "status": "resolved", "source_refs": [],
        "conclusion": "Only a constant-returning consumer was found; this says nothing about others.",
    }

    def transport(request):
        nonlocal handle
        payload = json.loads(request.content)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            reviews.append(payload)
            receipts = [json.loads(message["content"]) for message in payload["messages"]
                        if message["role"] == "tool" and message["tool_call_id"] in {"call-1", "call-3"}]
            if len(reviews) in {1, 3}:
                if len(reviews) == 3:
                    assert "return 42" in receipts[0]["content"]
                    assert payload["messages"][:len(reviews[-2]["messages"])] == reviews[-2]["messages"]
                    assert "record_investigation" in names
                name, result = "read_file", {
                    "path": "consumer.py", "revision": "head", "start": 1, "end": 2,
                }
            elif len(reviews) == 2:
                handle = receipts[0]["source_ref"]
                notes["source_refs"] = [handle]
                name, result = "record_investigation", {"note": notes}
            elif len(reviews) == 4:
                assert receipts[-1]["source_ref"] == handle
                assert receipts[-1]["content_omitted"] is True
                name, result = "record_investigation", {
                    "note": {**notes, "status": "open", "missing_premise": "Check other consumers."},
                }
            else:
                assert "return 42" in receipts[0]["content"]
                name, result = "UnitCompletion", {
                    "unresolved": completion_gap("仍需确认其他消费者的输入约束。"),
                }
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{len(reviews)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)},
                }],
            }}], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        })

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(reviews) == 5
    assert report.usage["model_calls"] == 6
    assert report.status == "partial"
    assert "unresolved_investigation" in report.reasons[0]
    assert report.findings == []
    assert report.units[0].tool_calls == 4
    state = json.loads(provider.evidence.artifacts.read_text(report.units[0].context_archive_uri))
    assert state["investigations"]["consumer-contract"]["status"] == "open"
    assert state["context_metrics"]["epoch"] == 0
    assert len(state["receipts"]) == 4


@pytest.mark.parametrize("thinking", [False, True])
async def test_capacity_epochs_restore_archived_query_and_source_through_http_tools(tmp_path, thinking):
    support = {
        f"branch_{branch}.py": "\n".join(
            f"ITEM_{index} = '{branch} alpha{index} beta{index} gamma{index} delta{index} epsilon{index}'"
            for index in range(100)) + "\n"
        for branch in range(4)
    }
    store, run, lease, provider, sessions = setup_source_run(tmp_path, support_sources=support)
    run.configuration.options.limits.context_tokens = 24576
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    reviews = []
    restored = []
    action_step = 0
    checkpoint_calls = 0
    recall_call_id = None
    reread_call_id = None

    def transport(request):
        nonlocal action_step, checkpoint_calls, recall_call_id, reread_call_id
        payload = json.loads(request.content)
        if thinking:
            assert payload["thinking"] == {"type": "enabled"}
            assert payload.get("tool_choice", "auto") == "auto"
            for previous in payload["messages"]:
                if previous["role"] == "assistant":
                    assert previous["reasoning_content"].startswith("synthetic-reasoning-")
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            reviews.append(payload)
            assert payload["tools"] == reviews[0]["tools"]
            checkpoint = " Context checkpoint:" in payload["messages"][-1]["content"]
            if not checkpoint:
                action_step += 1
            step = action_step
            if checkpoint:
                checkpoint_calls += 1
                name, result = "record_investigation", {"note": {
                    "question_id": "remaining-consumers", "question": "Which consumer premises remain?",
                    "status": "open", "source_refs": ["S1"],
                    "conclusion": "The ratio source is known; remaining consumer contracts need inspection.",
                    "missing_premise": "Read remaining consumers.",
                }}
            elif step == 1:
                name, result = "search_code", {"query": "ratio", "path": "a.py", "revision": "base"}
            elif step <= 5:
                name, result = "read_file", {
                    "path": f"branch_{step - 2}.py", "revision": "head", "start": 1, "end": 100,
                }
            elif step == 6:
                states = [json.loads(message["content"]) for message in payload["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")]
                ledger = next(state for state in states if "retrieval_receipts" in state)
                receipt = next(item for item in ledger["retrieval_receipts"] if item["tool"] == "search_code")
                assert receipt["arguments"]["revision"] == "base"
                assert receipt["arguments"]["path"] == "a.py"
                name, result = "recall_context", {"receipt_ref": receipt["receipt_ref"]}
                recall_call_id = f"step-{len(reviews)}"
            elif step == 7:
                receipt = next(json.loads(message["content"]) for message in payload["messages"]
                               if message.get("tool_call_id") == recall_call_id)
                assert receipt["original"]["result"]["source"] == "search_hint"
                assert receipt["original"]["arguments"]["revision"] == "base"
                restored.append("query")
                name, result = "read_file", {
                    "path": "branch_0.py", "revision": "head", "start": 1, "end": 100,
                }
                reread_call_id = f"step-{len(reviews)}"
            else:
                receipt = next(json.loads(message["content"]) for message in payload["messages"]
                               if message.get("tool_call_id") == reread_call_id)
                assert receipt["content"] == support["branch_0.py"]
                restored.append("source")
                name, result = "UnitCompletion", {
                    "unresolved": completion_gap("消费者的输入契约仍未查明。")}
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "reasoning_content": f"synthetic-reasoning-{len(reviews)}" if thinking else None,
                "tool_calls": [{
                    "id": f"step-{len(reviews)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)},
                }],
            }}], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        })

    model = OpenAICompatibleProvider(
        "fixture", "https://example.test/v1", "fixture",
        provider_name="deepseek" if thinking else "openai-compatible",
        thinking=thinking).agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert restored == ["query", "source"]
    assert checkpoint_calls >= 1
    assert len(reviews) == 8 + checkpoint_calls
    assert report.status == "partial"
    state = json.loads(provider.evidence.artifacts.read_text(report.units[0].context_archive_uri))
    assert state["context_metrics"]["epoch"] >= 1
    assert len(state["receipts"]) == 7 + checkpoint_calls
    with sessions() as session:
        assert len(list(session.scalars(select(ReviewRequestOrm)))) == report.usage["model_calls"] == 9 + checkpoint_calls


@pytest.mark.parametrize("unfinished", [False, True])
async def test_small_unit_checkpoint_keeps_tools_for_concrete_followup(tmp_path, unfinished):
    store, run, lease, provider, _ = setup_source_run(tmp_path, support_sources={
        "tests/test_ratio.py": "from a import ratio\n",
        "consumer.py": "from a import ratio\n",
    })
    reviews = []

    def transport(request):
        payload = json.loads(request.content)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            reviews.append(payload)
            if len(reviews) <= 2:
                name, result = "read_file", {
                    "path": "tests/test_ratio.py" if len(reviews) == 1 else "consumer.py",
                    "revision": "head", "start": 1, "end": 1,
                }
            elif len(reviews) == 3:
                name, result = "search_code", {"query": "ratio", "revision": "head"}
            elif len(reviews) == 4:
                assert "Small-unit coverage checkpoint" in payload["messages"][-1]["content"]
                assert "read_file" in names
                name, result = "read_file", {
                    "path": "a.py", "revision": "base", "start": 1, "end": 2,
                }
            else:
                assert "read_file" in names
                assert "Small-unit coverage checkpoint" not in payload["messages"][-1]["content"]
                name, result = "UnitCompletion", {
                    "unresolved": completion_gap("尚未查明零输入约束。") if unfinished else None,
                }
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{len(reviews)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)},
                }],
            }}], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        })

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == ("partial" if unfinished else "completed")
    assert len(reviews) == 5
    assert report.units[0].status == "completed"
    assert report.units[0].error_code is None
    for previous, current in zip(reviews, reviews[1:]):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]


@pytest.mark.parametrize("call_limit", [1, 3])
@pytest.mark.parametrize("unfinished", [False, True])
async def test_last_allowed_request_finishes_instead_of_starting_more_tools(
    tmp_path, call_limit, unfinished,
):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = call_limit
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []
    finalizations = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        elif names == {"UnitCompletion", "submit_candidate"}:
            finalizations.append(payload)
            name, result = "UnitCompletion", {
                "unresolved": completion_gap("尚未查明外部调用方的零输入约束。") if unfinished else None,
            }
        else:
            name, result = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(finalizations) == 1
    assert "Close now because of call_fuse" in finalizations[0]["messages"][-1]["content"]
    assert report.status == ("partial" if unfinished else "completed")
    assert report.units[0].status == "completed"
    assert report.units[0].error_code is None
    assert report.units[0].tool_calls == call_limit - 1
    assert report.overview.status == "ready"
    assert len(requests) == report.usage["model_calls"] == call_limit + 1
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "review" for ticket in tickets) == call_limit
        assert all(ticket.state == "settled" for ticket in tickets)


@pytest.mark.parametrize("verdict", ["confirmed", "insufficient", "invalid"])
@pytest.mark.parametrize("call_limit", [1, 3, 8])
async def test_verifier_final_request_preserves_admission_and_hard_limit(tmp_path, verdict, call_limit):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = call_limit
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []
    verifier_final = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        extra_calls = []
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        elif "UnitCompletion" in names:
            if any(message["role"] == "tool" for message in payload["messages"]):
                name, result = "UnitCompletion", {}
            else:
                handle = next(source["source_ref"] for material in packet["materials"]
                              for source in material["sources"] if source["source"]["side"] == "head")
                name, result = "submit_candidate", {"proposal": {
                    "claim": "零输入导致除零", "trigger": "x=0", "impact": "请求失败",
                    "source_refs": [handle],
                }}
                if call_limit == 1:
                    assert names == {"submit_candidate", "UnitCompletion"}
                    extra_calls.append({"id": "review-complete", "type": "function", "function": {
                        "name": "UnitCompletion", "arguments": "{}"}})
        elif names == {"VerificationOutput"}:
            verifier_final.append(payload)
            handles = [source["source_ref"] for material in packet["materials"]
                       for source in material["sources"]]
            name = "VerificationOutput"
            result = {} if verdict == "invalid" else {
                "verdict": verdict, "attribution": "introduced" if verdict == "confirmed" else "uncertain",
                "reason": "BASE 使用非零回退，HEAD 在零输入时除零。" if verdict == "confirmed"
                          else "尚未查明调用方是否允许零输入。",
                "source_refs": handles,
                "severity": "high" if verdict == "confirmed" else None,
                "author_comment": "零输入导致除零，请恢复回退或显式处理。" if verdict == "confirmed" else None,
            }
        else:
            name, result = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}, *extra_calls]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(verifier_final) == (2 if call_limit == 8 and verdict == "invalid" else 1)
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    assert len(report.findings) == (1 if verdict == "confirmed" else 0)
    check = next(item for item in report.units if item.kind == "verification")
    if verdict == "invalid":
        assert check.status == ("failed" if call_limit == 8 else "interrupted")
        assert check.error_code == (
            "agent_structured_output_missing" if call_limit == 8 else "agent_model_limit")
        assert candidate.verification is None
        assert report.status == "partial"
        assert report.usage["unknown_calls"] == 0
    else:
        assert check.status == "completed"
        assert candidate.verification.verdict == verdict
    assert next(item for item in report.units if item.kind == "review").status == "completed"
    verifier_calls = (7 if verdict == "invalid" else 6) if call_limit == 8 else call_limit
    assert len(requests) == report.usage["model_calls"] == (1 if call_limit == 1 else 2) + verifier_calls + 1
    if call_limit == 8:
        assert "tool_loop" in verifier_final[0]["messages"][-1]["content"]
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "verification" for ticket in tickets) == verifier_calls


@pytest.mark.parametrize("mode", ["recover", "repeat", "exhausted", "budget"])
async def test_plain_text_verifier_requires_bounded_structured_correction(tmp_path, mode, caplog):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = 1 if mode == "exhausted" else 12
    if mode == "budget":
        run.configuration.options.limits.total_model_calls = 3
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, verifier_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        calls, content = [], None
        if "OverviewOutput" in names:
            results = [("OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            })]
        elif "UnitCompletion" in names:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            results = [
                ("submit_candidate", {"proposal": {"claim": "零输入导致除零", "trigger": "x=0",
                    "impact": "请求失败", "source_refs": [handle]}}),
                ("UnitCompletion", {}),
            ]
        else:
            verifier_requests.append(payload)
            result = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                "reason": "BASE 使用非零回退，HEAD 在零输入时除零。",
                "source_refs": [source["source_ref"] for material in packet["materials"]
                                for source in material["sources"]],
                "author_comment": "零输入会触发除零，请恢复回退或显式处理。"}
            if len(verifier_requests) == 1 or mode == "repeat":
                # Valid-looking prose/JSON is still not an admitted tool result.
                content = "PRIVATE_MODEL_FREE_TEXT\n" + json.dumps(result)
                results = []
            else:
                assert names == {"VerificationOutput"}
                results = [("VerificationOutput", result)]
        for index, (name, result) in enumerate(results):
            calls.append({"id": f"call-{len(requests)}-{index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(result)}})
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(verifier_requests) == (1 if mode in {"exhausted", "budget"} else 2)
    assert len(report.candidates) == 1
    check = next(item for item in report.units if item.kind == "verification")
    if mode == "recover":
        assert report.status == "completed"
        assert check.status == "completed"
        assert len(report.findings) == 1
    elif mode == "budget":
        assert report.status == "partial"
        assert check.status == "interrupted"
        assert check.error_code == "model_call_limit"
        assert report.candidates[0].verification is None
        assert report.findings == []
    else:
        assert report.status == "partial"
        assert check.status == "failed"
        assert check.error_code == "agent_structured_output_missing"
        assert report.candidates[0].verification is None
        assert report.findings == []
    assert "PRIVATE_MODEL_FREE_TEXT" not in caplog.text
    assert report.usage["unknown_calls"] == 0
    assert report.usage["model_calls"] == len(requests)
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "verification" for ticket in tickets) == len(verifier_requests)
        assert all(ticket.state == "settled" for ticket in tickets)


@pytest.mark.parametrize("mode", [
    "recover", "repeat", "insufficient", "invented", "receipt", "exhausted", "budget",
    "request_failure", "unknown_usage", "investigate", "format_used",
])
async def test_missing_change_attribution_gets_one_correction_in_original_attempt(tmp_path, mode):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = 1 if mode == "exhausted" else 12
    if mode == "budget":
        run.configuration.options.limits.total_model_calls = 3
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, verifier_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        if "OverviewOutput" in names:
            results = [("OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            })]
        elif "UnitCompletion" in names:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            results = [
                ("submit_candidate", {"proposal": {"claim": "零输入导致除零", "trigger": "x=0",
                    "impact": "请求失败", "source_refs": [handle]}}),
                ("UnitCompletion", {}),
            ]
        else:
            verifier_requests.append(payload)
            sources = [source for material in packet["materials"] for source in material["sources"]]
            handles = [source["source_ref"] for source in sources]
            head = next(source["source_ref"] for source in sources if source["source"]["side"] == "head")
            result = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                "reason": "BASE 使用非零回退，HEAD 在零输入时除零。",
                "source_refs": [head], "author_comment": "请恢复零输入回退或显式处理。"}
            if len(verifier_requests) == 2:
                assert names == {"VerificationOutput"}
                if mode != "format_used":
                    guidance = next(message["content"] for message in payload["messages"]
                                    if isinstance(message["content"], str)
                                    and "VerificationOutput was rejected" in message["content"])
                    assert "missing_change_attribution_sources" in guidance
                    assert "Do not add unrelated BASE evidence" in guidance
                    assert "No more investigation is available" in guidance
                assert any(source["source"]["side"] == "base" for source in sources)
                if mode == "request_failure":
                    return httpx.Response(503, json={"error": {"message": "fixture failure"}})
                if mode not in {"repeat", "format_used"}:
                    result["source_refs"] = handles
                if mode == "insufficient":
                    result.update(verdict="insufficient", attribution="uncertain", severity=None,
                                  author_comment=None, reason="尚未确定零输入的调用契约。")
                elif mode in {"invented", "receipt"}:
                    result["source_refs"] = [head, "S999" if mode == "invented" else "R1"]
            results = [("read_file", {"path": "a.py", "revision": "base", "start": 1, "end": 2})
                       if mode == "investigate" and len(verifier_requests) == 2
                       else ("VerificationOutput", result)]
        response = {"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(requests)}-{index}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}
                    for index, (name, result) in enumerate(results)]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        if mode == "unknown_usage" and len(verifier_requests) == 2 and "VerificationOutput" in names:
            response.pop("usage")
        if mode == "format_used" and len(verifier_requests) == 1 and "VerificationOutput" in names:
            response["choices"][0].update(finish_reason="stop",
                message={"role": "assistant", "content": "零输入存在问题，尚未提交结构化结果。"})
        return httpx.Response(200, json=response)

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    expected_calls = 1 if mode in {"exhausted", "budget"} else 2
    assert len(verifier_requests) == expected_calls
    candidate = report.candidates[0]
    check = next(item for item in report.units if item.kind == "verification")
    assert check.status == "completed"
    assert check.attempt == 1
    assert candidate.verification.verdict == ("confirmed" if mode in {"recover", "unknown_usage"}
                                            else "insufficient")
    assert len(report.findings) == (1 if mode in {"recover", "unknown_usage"} else 0)
    if mode in {"invented", "receipt"}:
        assert candidate.admission_reason == "invalid_source_refs"
    elif mode == "insufficient":
        assert candidate.admission_reason is None
    elif mode not in {"recover", "unknown_usage"}:
        assert candidate.admission_reason == "missing_change_attribution_sources"
    assert report.usage["model_calls"] == len(requests)
    assert report.usage["unknown_calls"] == (1 if mode in {"request_failure", "unknown_usage"} else 0)
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        verification_tickets = [ticket for ticket in tickets if ticket.kind == "verification"]
        assert len(verification_tickets) == expected_calls
        assert {ticket.attempt_id for ticket in verification_tickets} == {check.attempt_id}


@pytest.mark.parametrize("boundary", [
    "long", "arguments", "line_arguments", "cursor", "loop", "loop_unresolved",
    "loop_refuses", "loop_corrects",
    "loop_invalid", "loop_unknown", "loop_recovers",
    "alternating_loop",
    "input_tokens", "output_tokens", "time",
])
async def test_tokens_and_time_guide_completion_before_call_fuse(tmp_path, boundary, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "rivu_model_request_timeout_seconds", 60)
    source = "def ratio(x):\n    return 10 / x\n" + "\n".join(
        f"VALUE_{index} = {index}" for index in range(30)) + "\n"
    store, run, lease, provider, sessions = setup_source_run(
        tmp_path, **({"head_source": source, "base_source": source.replace("10 / x", "10 / (x or 1)")}
                    if boundary in {"long", "cursor"} else {}))
    if boundary == "input_tokens":
        # Isolate token-driven closing from strict-call discovery buffers.
        run.configuration.options.limits.total_model_calls = None
        run.configuration.options.limits.total_input_tokens = OVERVIEW_INPUT + 8000
        run.pr.body = "预算调查上下文。" * 100
    elif boundary == "output_tokens":
        # One discovery, one possible first verification, and the overview.
        run.configuration.options.limits.total_output_tokens = 3 * 4096
    elif boundary == "time":
        run.review_cutoff = utcnow() + timedelta(seconds=30)
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, review_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            review_requests.append(payload)
            assert json.loads(payload["messages"][1]["content"])["depth"] == "standard"
            if len(review_requests) == 1:
                with sessions() as session:
                    reserved = session.scalar(select(ReviewRequestOrm).where(
                        ReviewRequestOrm.run_id == run.id, ReviewRequestOrm.kind == "review",
                        ReviewRequestOrm.state == "reserved"))
                    assert tokens({"messages": payload["messages"], "tools": payload["tools"]}) \
                        <= reserved.reserved_input_tokens
            if boundary == "cursor" and len(review_requests) <= 3:
                result = {"query": "VALUE_", "path": "" if len(review_requests) == 1 else "a.py",
                          "revision": "head", "limit": 1 if len(review_requests) < 3 else 40}
                if len(review_requests) == 2:
                    page = next(message for message in payload["messages"] if message["role"] == "tool")
                    result["cursor"] = json.loads(page["content"])["next_cursor"]
                name = "search_code"
            elif boundary == "loop_recovers" and len(review_requests) == 4:
                assert "read_file" in names
                assert "last 3 tool rounds repeated identical calls" in payload["messages"][-1]["content"]
                name, result = "read_file", {"path": "a.py", "revision": "base", "start": 1, "end": 2}
            elif boundary == "loop_refuses" or (
                "read_file" in names and len(review_requests) <= 13
                and (boundary != "arguments" or len(review_requests) <= 2)
                and (boundary != "loop_recovers" or len(review_requests) <= 3)
                and boundary != "cursor"
            ):
                start = (len(review_requests) + 5 if boundary == "long" else
                         1 + (len(review_requests) % 2) if boundary == "alternating_loop" else 1)
                name, result = "read_file", {"path": "a.py", "revision": "head",
                                             "start": start, "end": start + 1}
                if boundary == "loop_invalid" or (boundary == "arguments" and len(review_requests) == 1):
                    result.pop("end")
                elif boundary == "loop_unknown":
                    name, result = "unregistered_tool", {}
                elif boundary == "line_arguments" and len(review_requests) == 1:
                    result["end_line"] = result.pop("end")
            else:
                if boundary == "loop_corrects" and len(review_requests) == 6:
                    return httpx.Response(200, json={
                        "id": "chat", "object": "chat.completion", "created": 1,
                        "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Completed checks."}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
                name, result = "UnitCompletion", {
                    "unresolved": completion_gap("尚未查明调用方的零输入约束。")
                    if boundary == "loop_unresolved" else None,
                }
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": (
                    "I have a new idea." if boundary.startswith("loop") else None
                ), "tool_calls": [
                    {"id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == ("partial" if boundary in {"loop_unresolved", "loop_refuses"}
                            else "completed"), report.reasons
    assert len({payload["messages"][0]["content"] for payload in review_requests}) == 1
    for previous, current in zip(review_requests, review_requests[1:]):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]
    assert report.overview.status == "ready"
    assert report.units[0].error_code == ("agent_tool_loop" if boundary == "loop_refuses" else None)
    if boundary == "long":
        assert len(review_requests) == 14
        assert report.units[0].tool_calls == 13
    elif boundary == "arguments":
        assert len(review_requests) == 3
        assert report.units[0].tool_calls == 2
        error = next(message for message in review_requests[1]["messages"] if message["role"] == "tool")
        assert json.loads(error["content"])["error"] == "invalid_tool_arguments"
        assert "end" in json.loads(error["content"])["required_arguments"]
        assert "read_file" in {tool["function"]["name"] for tool in review_requests[-1]["tools"]}
    elif boundary == "line_arguments":
        error = next(message for message in review_requests[1]["messages"]
                     if message["role"] == "tool")
        assert json.loads(error["content"])["argument_corrections"] == {"end_line": "end"}
    elif boundary == "cursor":
        assert len(review_requests) == 4
        assert report.units[0].tool_calls == 3
        error = next(message for message in reversed(review_requests[2]["messages"])
                     if message["role"] == "tool")
        assert json.loads(error["content"])["error"] == "invalid_cursor"
        assert json.loads(error["content"])["retry_without_cursor"] is True
        assert "omit cursor" in json.loads(error["content"])["next_action"]
    elif boundary == "loop_recovers":
        assert len(review_requests) == 5
        assert report.units[0].status == "completed"
        assert report.units[0].tool_calls == 4
        assert "read_file" in {tool["function"]["name"] for tool in review_requests[-1]["tools"]}
    elif boundary == "alternating_loop":
        assert len(review_requests) == 11
        assert report.units[0].tool_calls == 10
        assert "cycles of 2 tool rounds" in review_requests[-1]["messages"][-1]["content"]
        assert {tool["function"]["name"] for tool in review_requests[-1]["tools"]} == {
            "submit_candidate", "UnitCompletion"}
    elif boundary.startswith("loop"):
        assert len(review_requests) == (7 if boundary in {"loop_refuses", "loop_corrects"} else 6)
        assert report.units[0].status == ("interrupted" if boundary == "loop_refuses" else "completed")
        assert report.units[0].tool_calls == (7 if boundary == "loop_refuses" else 5)
        assert "last 3 tool rounds repeated identical calls" in review_requests[3]["messages"][-1]["content"]
        for payload in review_requests[3:5]:
            assert "read_file" in {tool["function"]["name"] for tool in payload["tools"]}
        for payload in review_requests[5:]:
            assert {tool["function"]["name"] for tool in payload["tools"]} == {
                "submit_candidate", "UnitCompletion"}
            assert "tool_loop" in payload["messages"][-1]["content"]
        if boundary == "loop_unresolved":
            assert any("unresolved_investigation" in reason for reason in report.reasons)
    else:
        assert len(review_requests) == 1
        assert {tool["function"]["name"] for tool in review_requests[0]["tools"]} == {
            "submit_candidate", "UnitCompletion"}
        assert boundary in review_requests[0]["messages"][-1]["content"]
    assert report.usage["model_calls"] == len(requests) == len(review_requests) + 1


@pytest.mark.parametrize("role", ["review", "verification"])
@pytest.mark.parametrize("investigation", ["overlapping_reads", "empty_searches"])
async def test_low_novelty_keeps_tools_for_a_specific_followup(tmp_path, role, investigation):
    store, run, lease, provider, sessions = setup_source_run(tmp_path, support_sources={
        "caller.py": "\n".join(f"VALUE_{index} = {index}" for index in range(16)) + "\n",
    })
    investigation_requests = []
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        extra_calls = []
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        elif role == "verification" and "UnitCompletion" in names:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            name, result = "submit_candidate", {"proposal": {
                "claim": "零输入导致除零", "trigger": "x=0", "impact": "请求失败",
                "source_refs": [handle],
            }}
            extra_calls.append({"id": "review-complete", "type": "function", "function": {
                "name": "UnitCompletion", "arguments": "{}"}})
        else:
            investigation_requests.append(payload)
            step = len(investigation_requests)
            assert {"read_file", "search_code"} <= names
            if step == 1:
                assert not any("Runtime guidance (trusted host):" in (message.get("content") or "")
                               for message in payload["messages"])
            if step == 5 and investigation == "overlapping_reads":
                assert "Earlier source reads already covered" in payload["messages"][-1]["content"]
                assert "restore archived bodies when needed" in payload["messages"][-1]["content"]
            if investigation == "empty_searches":
                assert "Earlier source reads already covered" not in payload["messages"][-1]["content"]
            assert "Close now because of" not in payload["messages"][-1]["content"]
            if step <= 6 and investigation == "overlapping_reads":
                name, result = "read_file", {
                    "path": "caller.py", "revision": "head", "start": step, "end": 17 - step,
                }
            elif step <= 6:
                name, result = "search_code", {"query": f"missing_order_variant_{step}"}
            elif step == 7:
                name, result = "read_file", {
                    "path": "a.py", "revision": "base", "start": 1, "end": 2,
                }
            else:
                assert step == 8
                name = "UnitCompletion" if role == "review" else "VerificationOutput"
                result = {} if role == "review" else {
                    "verdict": "insufficient", "attribution": "uncertain",
                    "reason": "尚未查明调用方是否允许零输入。",
                }
        return httpx.Response(200, json={
            "id": "chat", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "Reconsidering the same source.",
                "tool_calls": [{"id": f"call-{len(requests)}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(result),
                }}, *extra_calls],
            }}], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
        })

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(investigation_requests) == 8
    work = next(item for item in report.units if item.kind == role)
    assert work.status == "completed"
    assert work.error_code is None
    assert work.tool_calls == 7
    assert report.findings == []
    if role == "verification":
        assert report.candidates[0].verification.verdict == "insufficient"
    for step, (previous, current) in enumerate(
        zip(investigation_requests, investigation_requests[1:]), 1,
    ):
        assert current["messages"][:len(previous["messages"])] == previous["messages"]
        result = json.loads(next(
            message for message in reversed(current["messages"]) if message["role"] == "tool"
        )["content"])
        if investigation == "empty_searches" and step <= 6:
            assert result["matches"] == []
            assert result["scope_complete"] is True
            assert result["next_cursor"] is None
        elif investigation == "overlapping_reads" and step > 1:
            assert result["content_omitted"] is True
    assert report.usage["model_calls"] == len(requests)
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert len(tickets) == len(requests)
        assert all(ticket.state == "settled" for ticket in tickets)
