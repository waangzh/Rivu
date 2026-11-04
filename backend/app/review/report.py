"""Deterministic facts first, followed by one bounded, separately saved overview."""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from app.llm.receipts import observe_response
from app.review.domain import Overview, OverviewDiagnostic, OverviewOutput, Report, SourceRef, utcnow
from app.review.findings import aggregate, group_findings
from app.review.overview_materials import overview_material
from app.review.planner import identity, tokens
from app.review.store import OVERVIEW_INPUT, BudgetExceeded, LeaseLost, ReviewStopped
from app.review.observability import message_content, stage

logger = logging.getLogger("Rivu.Review.Overview")


def validate_overview(raw, refs, finding_ids, attempt):
    finish_reason = raw.response_metadata.get("finish_reason")
    if finish_reason is not None and (not isinstance(finish_reason, str) or finish_reason not in {
            "stop", "length", "tool_calls", "content_filter", "function_call"}):
        finish_reason = "other"
    diagnostic = OverviewDiagnostic(
        attempt=attempt, finish_reason=finish_reason, tool_calls=len(raw.tool_calls),
        invalid_tool_calls=len(raw.invalid_tool_calls), content_present=bool(raw.content),
        reasoning_present=bool(raw.additional_kwargs.get("reasoning_content")))
    output = None
    if (raw.invalid_tool_calls or len(raw.tool_calls) != 1
            or raw.tool_calls[0]["name"] != "OverviewOutput"):
        diagnostic.error_code = "overview_schema"
    else:
        try:
            output = OverviewOutput.model_validate(raw.tool_calls[0]["args"])
        except ValidationError as exc:
            diagnostic.error_code = "overview_schema"
            diagnostic.validation_errors = exc.error_count()
        else:
            diagnostic.unknown_source_refs = sum(
                handle not in refs for paragraph in output.change_overview for handle in paragraph.source_refs)
            diagnostic.unknown_finding_ids = sum(key not in finding_ids for key in output.finding_order)
            if diagnostic.unknown_source_refs:
                diagnostic.error_code = "overview_unknown_source"
            elif diagnostic.unknown_finding_ids:
                diagnostic.error_code = "overview_unknown_finding"
    return (None if diagnostic.error_code else output), diagnostic


def overview_correction(raw, code):
    calls = [*raw.tool_calls, *raw.invalid_tool_calls]
    identifiers = [call.get("id") for call in calls]
    if (any(not isinstance(key, str) or not key for key in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        return None
    instruction = (
        "Runtime correction (trusted host): The previous overview was rejected with "
        f"{code}. Submit exactly one OverviewOutput tool call using the supplied schema. "
        "Use only source_refs from the original source_materials and finding_order IDs "
        "from the supplied final findings; use an empty finding_order if no ordering is needed. "
        "Do not copy demo references, return ordinary prose or call other tools. "
        "Correct the response using the original frozen materials; no further investigation "
        "or extra correction is available."
    )
    return [raw, *[
        ToolMessage(content=json.dumps({"ok": False, "error": code}),
                    tool_call_id=key, status="error") for key in identifiers
    ], HumanMessage(content=instruction)]


def build_report(run, work_items, candidates, usage):
    reasons = []
    reviews = [item for item in work_items if item.kind == "review"]
    reviewed_units = {item.unit_id for item in reviews}
    if run.plan.grouping_status == "pending":
        reasons.append("grouping_incomplete")
    reasons.extend(f"{unit.id}:not_started" for unit in run.plan.units
                   if unit.id not in reviewed_units)
    for item in reviews:
        if item.status != "completed":
            reasons.append(f"{item.unit_id}:{item.error_code or item.status}")
        if item.material_delivery != "complete":
            reasons.append(f"{item.unit_id}:material_incomplete")
        if item.has_unresolved_investigation:
            reasons.append(f"{item.unit_id}:unresolved_investigation")
    if any(candidate.verification is None for candidate in candidates):
        reasons.append("unverified_candidates")
    checked_history = {candidate.previous_candidate_id for candidate in candidates
                       if candidate.verification is not None}
    if any(item["candidate_id"] not in checked_history for item in run.plan.history):
        reasons.append("unverified_historical_candidates")
    status = "partial" if reasons else "completed"
    if reviews and all(item.status == "failed" for item in reviews) and not candidates:
        status = "failed"
    if not reviews and run.plan.grouping_status != "pending":
        reasons.append("scope_empty")
    findings = aggregate(candidates)
    facts = {"findings": findings, "finding_groups": group_findings(candidates),
             "candidates": candidates, "units": work_items,
             "excluded": run.plan.excluded, "status": status, "reasons": reasons, "usage": usage}
    return Report(version=identity(facts), **facts)


async def generate_overview(report, provider, budget, model):
    with stage("review.overview", kind="overview") as observation:
        result = await _generate_overview(report, provider, budget, model)
        observation.set(status=result.status, omitted_sources=result.omitted.get("sources", 0))
        return result


async def _generate_overview(report, provider, budget, model):
    started = perf_counter()
    run_id = provider.run.id
    previous = provider.run.report
    if not provider.run.configuration.options.generate_overview:
        logger.info("总览跳过 review_id=%.8s reason=disabled", run_id)
        return Overview(status="skipped_disabled")
    if not provider.run.plan.units:
        logger.info("总览跳过 review_id=%.8s reason=scope_empty", run_id)
        return Overview(status="skipped_empty")
    if utcnow() >= provider.run.hard_deadline:
        logger.warning("总览跳过 review_id=%.8s reason=hard_deadline", run_id)
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    prompt = (Path(__file__).parent / "prompts" / "overview.md").read_text(encoding="utf-8")
    schema = OverviewOutput.model_json_schema()
    limits = provider.run.configuration.options.limits
    input_limit = max(0, min(OVERVIEW_INPUT, limits.context_tokens - limits.output_tokens))
    # Keep the original request margins proportional, including smaller contexts.
    request_overhead = input_limit * 512 // 6000
    material_headroom = input_limit * 128 // 6000
    material_limit = max(0, input_limit - tokens([prompt, schema]) - request_overhead - material_headroom)
    try:
        packet, refs, omissions = await asyncio.to_thread(
            overview_material, report, provider, max_tokens=material_limit)
    except BudgetExceeded:
        return Overview(status="skipped_deadline", error_code="hard_deadline")
    except (ValueError, OSError):
        logger.warning("总览材料不可用 review_id=%.8s", run_id)
        return Overview(status="failed", error_code="overview_material_unavailable")
    input_hash = identity({"correction_limit": 1, "prompt": prompt, "schema": schema, "materials": packet,
                          "omissions": [item.model_dump() for item in omissions]})
    if (previous and previous.overview.input_hash == input_hash
            and previous.overview.status in {"ready", "failed"}):
        logger.info("总览复用 review_id=%.8s status=%s", run_id, previous.overview.status)
        return previous.overview
    overview = Overview(status="running", input_hash=input_hash, omitted=packet["omitted"],
                        omitted_materials=omissions)
    logger.info("总览材料准备完成 review_id=%.8s changes=%d sources=%d material_tokens=%d "
                "omitted_changes=%d omitted_sources=%d", run_id, len(packet["changes"]),
                len(refs), tokens(packet), packet["omitted"]["changes"], packet["omitted"]["sources"])
    report.overview = overview
    await asyncio.to_thread(provider.store.save_report, provider.run.id, report, provider.lease)
    ticket, receipt = None, None
    try:
        messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(packet, ensure_ascii=False))]
        structured = model.bind_tools([OverviewOutput], tool_choice="OverviewOutput")
        for attempt in (1, 2):
            receipt = None
            await budget.active()
            estimate = tokens({
                "messages": [message.model_dump(mode="json") for message in messages],
                "schema": schema,
            }) + request_overhead
            ticket = await budget.reserve(None, estimate, overview=True)
            logger.info("总览模型请求开始 review_id=%.8s ticket=%.8s attempt=%d estimated_input_tokens=%d",
                        run_id, ticket, attempt, estimate)
            seconds = max(0, (provider.run.hard_deadline - utcnow()).total_seconds())
            with observe_response() as observed_receipt, stage(
                    "model.request", run_type="llm", ticket=ticket, attempt=attempt) as observation:
                receipt = observed_receipt
                observation.content(inputs={"messages": message_content(messages)})
                async with asyncio.timeout(seconds):
                    raw = await structured.ainvoke(messages)
                observation.usage(getattr(raw, "usage_metadata", None))
                observation.content(outputs={"messages": message_content([raw])})
            usage = getattr(raw, "usage_metadata", None)
            await budget.settle(ticket, usage)
            logger.info(
                "总览模型请求返回 review_id=%.8s ticket=%.8s input_tokens=%s output_tokens=%s elapsed_ms=%.0f",
                run_id, ticket, usage["input_tokens"] if usage is not None else "unknown",
                usage["output_tokens"] if usage is not None else "unknown",
                (perf_counter() - started) * 1000)
            ticket = None
            output, diagnostic = validate_overview(raw, refs, {finding.id for finding in report.findings}, attempt)
            overview.diagnostics.append(diagnostic)
            logger.info(
                "总览响应形状 review_id=%.8s attempt=%d code=%s finish_reason=%s tool_calls=%d "
                "invalid_calls=%d content_present=%s reasoning_present=%s validation_errors=%d "
                "unknown_sources=%d unknown_findings=%d",
                run_id, diagnostic.attempt, diagnostic.error_code or "-", diagnostic.finish_reason or "-",
                diagnostic.tool_calls, diagnostic.invalid_tool_calls, diagnostic.content_present,
                diagnostic.reasoning_present, diagnostic.validation_errors,
                diagnostic.unknown_source_refs, diagnostic.unknown_finding_ids)
            await budget.active()
            if output is not None:
                overview.status, overview.output = "ready", output
                overview.sources = [SourceRef.model_validate(ref) for ref in refs.values()]
                logger.info("总览生成完成 review_id=%.8s paragraphs=%d omitted_sources=%d",
                            run_id, len(output.change_overview), overview.omitted.get("sources", 0))
                break
            overview.error_code = diagnostic.error_code
            correction = overview_correction(raw, diagnostic.error_code) if attempt == 1 else None
            if correction is None:
                overview.status = "failed"
                break
            report.overview = overview
            await asyncio.to_thread(provider.store.save_report, run_id, report, provider.lease)
            messages.extend(correction)
            logger.warning("总览将在原预算内纠正一次 review_id=%.8s code=%s", run_id, diagnostic.error_code)
    except (LeaseLost, ReviewStopped, asyncio.CancelledError) as exc:
        logger.info("总览停止 review_id=%.8s reason=%s", run_id, type(exc).__name__)
        raise
    except Exception as exc:
        if ticket:
            await budget.settle(ticket, receipt.usage if receipt else None)
        overview.status = "skipped_deadline" if utcnow() >= provider.run.hard_deadline else "failed"
        overview.error_code = ("hard_deadline" if overview.status == "skipped_deadline" else
                               str(exc) if isinstance(exc, BudgetExceeded) else
                               "overview_timeout" if isinstance(exc, TimeoutError) else "overview_unavailable")
        logger.warning("总览未完成 review_id=%.8s status=%s code=%s error_type=%s elapsed_ms=%.0f",
                       run_id, overview.status, overview.error_code, type(exc).__name__,
                       (perf_counter() - started) * 1000)
    if overview.status == "ready":
        overview.error_code = None
    return overview
