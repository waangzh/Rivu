"""LangChain owns the tool loop; each actual model request needs a durable ticket."""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelResponse, hook_config
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.config import settings
from app.review.context import AgentContext, RepeatedToolLoop, TOOL_LOOP_LIMIT, TOOL_LOOP_WARNING
from app.review.domain import UnitCompletion, VerificationOutput, utcnow
from app.review.findings import admit_verification
from app.review.planner import tokens
from app.review.observability import event, message_content, review_trace, stage
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped, verification_input_limit
from app.review.tools import (
    READ_TOOLS, read_discussion, recall_context, record_investigation, submit_candidate,
)
from app.review.working_context import WorkingContext

logger = logging.getLogger("Rivu.Review.Agent")


class MissingStructuredResult(ValueError):
    pass


def safe_error_detail(exc):
    # Allow only known type/code pairs, never arbitrary exception bodies.
    allowed = {
        ValueError: {"source_not_visible_in_this_attempt"},
        BudgetExceeded: {
            "hard_deadline", "review_cutoff", "agent_model_limit", "model_call_limit",
            "input_token_limit", "context_capacity", "output_token_limit", "candidate_limit",
        },
        RepeatedToolLoop: {"agent_tool_loop"},
    }
    codes = allowed.get(type(exc))
    if codes is None:
        return "-"
    detail = str(exc)
    return detail if detail in codes else "-"


def request_input_tokens(messages, tools, schema):
    return tokens({
        "messages": [message.model_dump(mode="json") for message in messages],
        "tools": [convert_to_openai_tool(tool) for tool in tools],
        "response_format": schema.model_json_schema(),
    }) + 512


def completion_correction(context):
    if context.structured_output_retry:
        raise MissingStructuredResult
    if context.model_calls >= context.provider.run.configuration.options.limits.model_calls:
        raise BudgetExceeded("agent_model_limit")
    context.structured_output_retry = True
    context.structured_correction_limit = context.model_calls + 1
    event("agent.completion_correction")
    return (
        "Runtime correction (trusted host): Return UnitCompletion once using the current schema. "
        "No further investigation is available. Set unresolved=null when necessary checks "
        "are complete, including when only unsupported possibilities remain. Otherwise supply "
        "required_check, a source-supported observation, missing_premise, why_required and "
        "received S source_refs. Explain why this missing fact actually blocks a necessary "
        "check; lack of proof that every conceivable failure is absent is not a blocker. "
        "Do not copy a summary or refuted hypothesis into a gap, or assume completion to "
        "avoid describing a real gap. R receipts and invented S handles are not source evidence."
    )


class RequestGate(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        context = request.runtime.context
        await context.check_active()
        result = await handler(request)
        # Schema and unknown-tool errors happen before @bounded executes.
        if isinstance(result, ToolMessage) and result.status == "error":
            context.tool_calls += 1
            fields = request.tool.tool_call_schema.model_fields if request.tool else {}
            error = ("tool_call_limit"
                     if context.tool_calls > context.provider.run.configuration.options.limits.tool_calls
                     else "invalid_tool_arguments" if request.tool else "unknown_tool")
            body = {
                "ok": False, "error": error,
                "required_arguments": [name for name, field in fields.items() if field.is_required()],
                "next_action": "Correct the arguments using the tool schema. Do not repeat "
                               "the same rejected call. Return the required result tool if finished.",
            }
            if request.tool_call["name"] == "read_file":
                corrections = {name: replacement for name, replacement in (
                    ("start_line", "start"), ("end_line", "end"))
                    if name in request.tool_call["args"]}
                if corrections:
                    body["argument_corrections"] = corrections
            context.note_tool_result(request.tool_call["name"], request.tool_call["args"], body)
            event("tool.rejected", tool=request.tool_call["name"], reason=error, ok=False)
            result = ToolMessage(content=json.dumps(body), tool_call_id=request.tool_call["id"],
                                 name=request.tool_call["name"], status="error")
        await context.check_active()
        if isinstance(result, ToolMessage):
            fields = request.tool.tool_call_schema.model_fields if request.tool else {}
            arguments = {name: field.get_default(call_default_factory=True)
                         for name, field in fields.items() if not field.is_required()}
            arguments.update(request.tool_call["args"])
            result = await context.archive_tool(
                request.tool_call["name"], arguments, result)
        return result

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        if state.get("structured_response") is not None:
            return None
        message = state["messages"][-1]
        if not isinstance(message, AIMessage) or message.tool_calls:
            return None
        context = runtime.context
        if (context.structured_output_retry
                or context.model_calls >= context.provider.run.configuration.options.limits.model_calls):
            raise MissingStructuredResult
        context.structured_output_retry = True
        event("agent.format_correction")
        schema_name = "UnitCompletion" if context.work.kind == "review" else "VerificationOutput"
        logger.warning(
            "模型未交付结构化结果，将在原预算内纠正一次 review_id=%.8s work_id=%.8s expected=%s",
            context.provider.run.id, context.work.id, schema_name)
        return {"messages": [HumanMessage(content=(
            f"Your previous reply did not call the required result tool. Return {schema_name} "
            "via its tool now, using the source_ref handles already received. "
            "Do not return prose or JSON as ordinary chat content. "
            "No additional investigation is needed to correct the response format."
        ))], "jump_to": "model"}

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        await context.check_active()
        if (context.structured_correction_limit is not None
                and context.model_calls >= context.structured_correction_limit):
            raise MissingStructuredResult
        repeated = context.begin_round()
        if context.loop_closing_calls >= 2:
            raise RepeatedToolLoop("agent_tool_loop")
        limits = context.provider.run.configuration.options.limits
        schema = request.response_format.schema
        tools = request.tools
        def estimate_packet(messages):
            return request_input_tokens(
                ([request.system_message] if request.system_message else []) + messages,
                tools, schema) + 1024

        input_limit = limits.context_tokens - limits.output_tokens
        messages = context.window.project(context, request.messages, estimate_packet, input_limit)
        await context.persist()
        # Reserve the full packet, including all tools and room for budget guidance.
        estimate = estimate_packet(messages)
        try:
            ticket, available = await context.budget.reserve(context.work, estimate, with_remaining=True)
        except BudgetExceeded as exc:
            if str(exc) in {"context_capacity", "input_token_limit"}:
                messages = context.window.project(
                    context, request.messages, estimate_packet, input_limit, force=True)
                estimate = estimate_packet(messages)
                ticket, available = await context.budget.reserve(context.work, estimate, with_remaining=True)
            else:
                raise
        context.model_calls += 1
        remaining = available["agent_model_calls"]
        call_remaining = min(remaining, available["model_calls"]) if available["model_calls"] is not None else remaining
        run = context.provider.run
        deadline = run.review_cutoff if context.work.kind == "review" else run.hard_deadline
        seconds = max(0, (deadline - utcnow()).total_seconds())
        closing_reasons = []
        if repeated >= TOOL_LOOP_LIMIT or context.loop_closing_calls:
            closing_reasons.append("tool_loop")
        if (available["input_tokens"] is not None
                and available["input_tokens"] < 2 * estimate):
            closing_reasons.append("input_tokens")
        if (available["output_tokens"] is not None
                and available["output_tokens"] < 2 * limits.output_tokens):
            closing_reasons.append("output_tokens")
        if seconds <= settings.rivu_model_request_timeout_seconds:
            closing_reasons.append("time")
        if call_remaining <= 1:
            closing_reasons.append("call_fuse")
        if context.tool_calls >= limits.tool_calls:
            closing_reasons.append("tool_limit")
        if context.structured_output_retry:
            closing_reasons.append("format_correction")
        closing = bool(closing_reasons)
        context.investigation_closed = closing
        if "tool_loop" in closing_reasons:
            context.loop_closing_calls += 1
        if closing:
            tools = [tool for tool in tools if tool.name == "submit_candidate"]
        if context.tool_calls >= limits.tool_calls:
            tools = []
        instruction = ""
        if context.window.checkpoint_pending and not closing:
            instruction += (
                " Context checkpoint: before more investigation, call record_investigation to "
                "preserve the questions already checked, their scoped conclusions, counterevidence "
                "and the specific questions/premises still open. Link all relevant source_refs and "
                "prerequisite questions. This is necessary to continue after old history is archived. "
                "Tool definitions stay unchanged, but investigation execution pauses until the note "
                "is saved. If the necessary investigation "
                "is already complete, return the final result directly instead of creating notes."
            )
        if repeated >= TOOL_LOOP_WARNING:
            instruction += (
                f" The last {repeated} tool rounds repeated identical calls with identical results. "
                if context.repeated_cycle_length == 1 else
                f" The last {repeated} cycles of {context.repeated_cycle_length} tool rounds "
                "repeated identical calls with identical results. "
            )
            instruction += (
                "Reuse those results or change the specific request to resolve a missing premise. "
                "Repetition does not establish whether the investigation is complete."
            )
        elif (context.repeated_request_count >= TOOL_LOOP_WARNING
                and context.repeated_request_count > context.repetition_notice_sent):
            context.repetition_notice_sent = context.repeated_request_count
            instruction += (
                f" An identical request has returned the same result "
                f"{context.repeated_request_count} times, including interleaved checks. "
                "Reuse received evidence. State the specific missing premise before another read. "
                "If multiple branches remain, preserve their questions and counterevidence with "
                "record_investigation; if the concrete claim is decided, return the result. "
                "Repeated data alone neither proves completion nor refutes an unresolved claim."
            )
        if context.repeated_source_read_count - context.source_read_notice_sent >= TOOL_LOOP_WARNING:
            context.source_read_notice_sent = context.repeated_source_read_count
            instruction += (
                f" Earlier source reads already covered the full ranges of "
                f"{context.repeated_source_read_count} later reads, even where range arguments differed. "
                "Identify the specific missing premise before another overlapping read. "
                "Reuse source bodies still present; restore archived bodies when needed. "
                "This is an efficiency reminder, not evidence that a question is answered "
                "or a reason to stop a necessary investigation."
            )
        if call_remaining == 2:
            instruction += (
                " The next request is reserved for your final structured result. "
                "Submit supported candidates now; use any remaining reads only for a critical missing premise."
            )
        if context.small_unit_ready_for_checkpoint() and not context.coverage_checkpoint_sent:
            context.coverage_checkpoint_sent = True
            instruction += (
                " Small-unit coverage checkpoint: all required change materials, related test source "
                "and consumer context have been received, together with a successful relationship search. "
                "These observations are not proof that every necessary question is answered. "
                "Return UnitCompletion now if the necessary checks are complete. Otherwise use the "
                "available tools only for a specific remaining premise; do not enumerate unrelated "
                "packages or transitive consumers. New relevant evidence may continue the investigation."
            )
        if closing:
            instruction += (
                f" Close now because of {','.join(closing_reasons)}. "
                f"No investigation tools are available. Return {schema.__name__} now using the "
                "evidence already received. If a necessary premise is still missing, "
                "describe it in the structured unresolved gap (Reviewer) or return insufficient "
                "(Verifier). Never present unfinished investigation as a clean result."
            )
            if tools:
                instruction += (
                    " If you have a newly supported claim, call submit_candidate AND "
                    "UnitCompletion in this same response so it is saved before this attempt ends."
                )
            if "tool_loop" in closing_reasons:
                instruction += (
                    " Repetition alone is not a missing premise. Assess the evidence already received. "
                    "This is a bounded finalization opportunity, not permission to assume success."
                )
                instruction += (
                    " Finish with unresolved=null if the necessary checks are complete; "
                    "otherwise identify the specific unanswered question."
                    if context.work.kind == "review" else
                    " Confirm or refute only with the required supporting evidence; "
                    "otherwise return insufficient and identify the missing premise."
                )
        guidance = (HumanMessage(content="Runtime guidance (trusted host):" + instruction,
                                 id=f"runtime-{ticket}") if instruction else None)
        request = request.override(tools=tools, messages=[*messages, *([guidance] if guidance else [])])
        started = perf_counter()
        logger.info(
            "模型请求开始 review_id=%.8s work_id=%.8s kind=%s attempt_id=%.8s ticket=%.8s "
            "call=%d/%d estimated_input_tokens=%d remaining_input_tokens=%s remaining_output_tokens=%s "
            "remaining_seconds=%.0f closing=%s closing_reason=%s",
            context.provider.run.id, context.work.id, context.work.kind, context.work.attempt_id,
            ticket, context.model_calls, limits.model_calls, estimate, available["input_tokens"],
            available["output_tokens"], seconds, closing, ",".join(closing_reasons) or "-")
        logger.info(
            "上下文投影 review_id=%.8s work_id=%.8s original_tokens=%d projected_tokens=%d "
            "saved_tokens=%d investigation_questions=%d epoch=%d compacted=%s pressure=%s "
            "shared_message_prefix_tokens=%d pressure_reason=%s minimum_reclaim_tokens=%d "
            "restored_sources=%d",
            run.id, context.work.id, context.window.metrics["original_input_tokens"],
            context.window.metrics["projected_input_tokens"], context.window.metrics["saved_input_tokens"],
            len(context.investigations), context.window.epoch, context.window.metrics["compacted"],
            context.window.metrics["pressure"], context.window.metrics["shared_message_prefix_tokens"],
            context.window.metrics["pressure_reason"] or "-",
            context.window.metrics["minimum_reclaim_tokens"], context.window.metrics["restored_sources"])
        pending, hunks = dict(context.pending), dict(context.pending_hunks)
        try:
            with stage("model.request", run_type="llm", ticket=ticket,
                       call=context.model_calls, estimated_input_tokens=estimate,
                       remaining_input_tokens=available["input_tokens"],
                       remaining_output_tokens=available["output_tokens"],
                       remaining_seconds=seconds, closing=closing) as observation:
                observation.content(inputs={"messages": message_content(
                    ([request.system_message] if request.system_message else []) + request.messages)})
                event("context.projected", **context.window.metrics,
                      investigation_questions=len(context.investigations),
                      repeated_rounds=repeated,
                      repeated_cycle_length=context.repeated_cycle_length,
                      repeated_request_count=context.repeated_request_count,
                      repeated_source_read_count=context.repeated_source_read_count)
                for reason in closing_reasons:
                    event("agent.closing", reason=reason)
                response = await handler(request)
                observed_message = next(
                    (item for item in reversed(response.result) if isinstance(item, AIMessage)), None)
                observation.usage(getattr(observed_message, "usage_metadata", None))
                observation.content(outputs={"messages": message_content(response.result)})
        except asyncio.CancelledError:
            logger.info("模型请求取消 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f",
                        context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000)
            raise
        except Exception as exc:
            await context.budget.settle(ticket, None)
            logger.warning(
                "模型请求失败 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f error_type=%s usage=unknown",
                context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000,
                type(exc).__name__)
            raise
        message = next((item for item in reversed(response.result) if isinstance(item, AIMessage)), None)
        usage = getattr(message, "usage_metadata", None)
        context.window.observe_response(
            usage["input_tokens"] if usage else None, usage["output_tokens"] if usage else None,
            perf_counter() - started, settings.rivu_model_request_timeout_seconds)
        await context.budget.settle(ticket, usage)
        called_tools = {call["name"] for call in message.tool_calls} if message else set()
        tool_names = [tool.name for tool in tools if tool.name in called_tools]
        if schema.__name__ in called_tools:
            tool_names.append(schema.__name__)
        logger.info(
            "模型请求返回 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f "
            "input_tokens=%s output_tokens=%s tool_calls=%d tools=%s",
            context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000,
            usage["input_tokens"] if usage is not None else "unknown",
            usage["output_tokens"] if usage is not None else "unknown",
            len(message.tool_calls) if message else 0, ",".join(tool_names) or "-")
        await context.check_active()
        # A successful response proves the model received the full request packet.
        await context.acknowledge(pending, hunks)
        await context.persist()
        # Keep any guidance at its original position in the append-only history.
        return ModelResponse(result=[*([guidance] if guidance else []), *response.result],
                             structured_response=response.structured_response)


class AgentRunner:
    def __init__(self, model):
        self.model = model

    async def _admit_completion(self, agent, result, output, context):
        try:
            if output.unresolved:
                context.resolve(output.unresolved.source_refs)
            return output
        except ValueError:
            message = HumanMessage(content=completion_correction(context))
        corrected = await agent.ainvoke(
            {"messages": [*result["messages"], message], "structured_response": None},
            context=context, config={"recursion_limit": 12})
        revised = corrected.get("structured_response")
        if not isinstance(revised, UnitCompletion):
            raise MissingStructuredResult
        await context.budget.active()
        if revised.unresolved:
            try:
                context.resolve(revised.unresolved.source_refs)
            except ValueError as exc:
                raise MissingStructuredResult from exc
        return revised

    async def _correct_verification(self, agent, result, output, candidate, context):
        if (candidate.admission_reason not in {
                "invalid_source_refs", "missing_decision_sources", "missing_change_attribution_sources"}
                or context.structured_output_retry
                or context.model_calls >= context.provider.run.configuration.options.limits.model_calls):
            return output, candidate
        problem = candidate.admission_reason
        context.structured_output_retry = True
        context.structured_correction_limit = context.model_calls + 1
        event("agent.verification_correction", reason=problem)
        logger.warning(
            "复核引用未通过校验，将在原预算内纠正一次 review_id=%.8s work_id=%.8s reason=%s",
            context.provider.run.id, context.work.id, problem)
        message = HumanMessage(content=(
            f"Runtime correction (trusted host): VerificationOutput was rejected: {problem}. "
            "Return VerificationOutput once more using only actual S source_ref handles from "
            "source bodies already received. R receipt_ref handles identify retrieval results "
            "and MUST NOT appear in source_refs. Do not treat search results as decision evidence. "
            "No more investigation is available. Reassess the existing source: confirm/refute "
            "only when it supports that decision; otherwise return insufficient with the specific "
            "missing premise. For introduced/exposed attribution in modified or renamed code, "
            "cite the received BASE and HEAD source bodies relevant to the claimed causal change. "
            "Do not add unrelated BASE evidence merely to satisfy the side check. If the received "
            "sources cannot establish that change, return insufficient and explain the gap. "
            "An insufficient result containing a question must also cite "
            "the actual S source_ref handles supporting its observation. If no received source "
            "supports an author question, leave question null and explain the missing premise. "
            "Do not simply preserve your previous verdict. "
            f"Authorized source handles include: {', '.join(list(context.visible)[:64])}."
        ))
        try:
            corrected = await agent.ainvoke(
                {"messages": [*result["messages"], message], "structured_response": None},
                context=context, config={"recursion_limit": 12})
            revised = corrected.get("structured_response")
            if not isinstance(revised, VerificationOutput):
                raise MissingStructuredResult
            await context.budget.active()
            candidate = admit_verification(candidate, revised, context)
            return revised, candidate
        except (LeaseLost, ReviewStopped, asyncio.CancelledError):
            raise
        except Exception as exc:
            # Keep the rejected result if correction cannot finish; never bypass admission.
            logger.warning(
                "复核引用纠正未完成 review_id=%.8s work_id=%.8s error_type=%s detail=%s",
                context.provider.run.id, context.work.id, type(exc).__name__, safe_error_detail(exc))
            event("agent.verification_correction_failed", reason=problem, error_type=type(exc).__name__)
            return output, candidate

    async def run(self, work, provider, budget):
        with review_trace(provider.run, provider.lease):
            with stage("agent.attempt", work_id=work.id, unit_id=work.unit_id,
                       attempt_id=work.attempt_id, attempt=work.attempt, kind=work.kind):
                return await self._run(work, provider, budget)

    async def _run(self, work, provider, budget):
        context = AgentContext(provider=provider, work=work, budget=budget, window=WorkingContext())
        candidate = None
        started = perf_counter()
        logger.info("Agent开始 review_id=%.8s work_id=%.8s unit_id=%.8s kind=%s attempt=%d attempt_id=%.8s",
                    provider.run.id, work.id, work.unit_id, work.kind, work.attempt, work.attempt_id)
        try:
            role = "reviewer" if work.kind == "review" else "verifier"
            tools = [*READ_TOOLS, record_investigation, recall_context]
            if role == "reviewer":
                tools.append(submit_candidate)
            if provider.run.configuration.options.include_pr_discussions and provider.discussions:
                tools.append(read_discussion)
            prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text(encoding="utf-8")
            schema = UnitCompletion if role == "reviewer" else VerificationOutput
            if work.kind == "verification":
                candidate = next(item for item in await asyncio.to_thread(
                    provider.store.candidates, provider.run.id) if item.id == work.candidate_id)
            limits = provider.run.configuration.options.limits
            request_budget = context.window.initial_budget(limits)
            if role == "verifier":
                usage = await asyncio.to_thread(provider.store.usage, provider.run.id)
                request_budget = min(context.window.initial_ceiling(limits),
                    verification_input_limit(limits, usage["committed_input_tokens"],
                                             overview=provider.run.configuration.options.generate_overview))
            if limits.total_input_tokens is not None:
                headroom = await budget.headroom(work)
                request_budget = min(request_budget, max(1, headroom["input_tokens"] - 1024))
            initial_message = HumanMessage(content="", id=work.attempt_id)
            def estimate_initial(packet):
                return request_input_tokens([SystemMessage(content=prompt),
                    initial_message.model_copy(update={
                        "content": json.dumps(packet, ensure_ascii=False)})], tools, schema)
            initial = await provider.initial(context, candidate, request_budget=request_budget,
                                             estimate_request=estimate_initial)
            context.initial_packet = initial
            response_format = (ToolStrategy(schema, handle_errors=lambda error: completion_correction(context))
                               if role == "reviewer" else ToolStrategy(schema))
            agent = create_agent(self.model, tools=tools, system_prompt=prompt,
                response_format=response_format, middleware=[RequestGate()],
                context_schema=AgentContext, name=role)
            initial_message.content = json.dumps(initial, ensure_ascii=False)
            result = await agent.ainvoke(
                {"messages": [initial_message]},
                context=context, config={"recursion_limit": limits.model_calls * 8 + 24})
            output = result.get("structured_response")
            if not isinstance(output, schema):
                raise MissingStructuredResult
            await budget.active()
            if candidate:
                candidate = admit_verification(candidate, output, context)
                output, candidate = await self._correct_verification(
                    agent, result, output, candidate, context)
            else:
                output = await self._admit_completion(agent, result, output, context)
            # ainvoke only returns after all tool tasks and their DB receipts finish.
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "completed", output.model_dump(), None, provider.lease,
                candidate=candidate, tool_calls=context.tool_calls)
            if candidate:
                event("candidate.verified", candidate_id=candidate.id, model_verdict=output.verdict,
                      verdict=candidate.verification.verdict, reason=candidate.admission_reason,
                      source_count=len(candidate.verification_sources),
                      finding=candidate.finding is not None)
            event("agent.finished", status="completed", model_calls=context.model_calls,
                  tool_calls=context.tool_calls,
                  unresolved=output.unresolved is not None if isinstance(output, UnitCompletion) else False)
            logger.info(
                "Agent结果已提交 review_id=%.8s work_id=%.8s kind=%s elapsed_ms=%.0f "
                "model_calls=%d tool_calls=%d verdict=%s finding=%s unresolved=%s",
                provider.run.id, work.id, work.kind, (perf_counter() - started) * 1000,
                context.model_calls, context.tool_calls,
                candidate.verification.verdict if candidate else "-",
                candidate.finding is not None if candidate else False,
                output.unresolved is not None if isinstance(output, UnitCompletion) else False)
        except (LeaseLost, ReviewStopped, asyncio.CancelledError) as exc:
            event("agent.stopped",
                  status="cancelled" if isinstance(exc, asyncio.CancelledError) else "interrupted",
                  reason="cancelled" if isinstance(exc, asyncio.CancelledError)
                  else "lease_lost" if isinstance(exc, LeaseLost) else "review_stopped")
            logger.info("Agent停止 review_id=%.8s work_id=%.8s reason=%s",
                        provider.run.id, work.id, type(exc).__name__)
            raise
        except Exception as exc:
            if isinstance(exc, RepeatedToolLoop):
                code = "agent_tool_loop"
            elif isinstance(exc, BudgetExceeded):
                code = str(exc)
            elif isinstance(exc, MissingStructuredResult):
                code = "agent_structured_output_missing"
            else:
                code = "agent_execution_failed"
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "interrupted" if isinstance(exc, (BudgetExceeded, RepeatedToolLoop)) else "failed", None, code,
                provider.lease, tool_calls=context.tool_calls)
            event("agent.finished",
                  status="interrupted" if isinstance(exc, (BudgetExceeded, RepeatedToolLoop)) else "failed",
                  reason=code, model_calls=context.model_calls, tool_calls=context.tool_calls)
            logger.warning(
                "Agent未完成 review_id=%.8s work_id=%.8s kind=%s code=%s error_type=%s detail=%s "
                "model_calls=%d tool_calls=%d elapsed_ms=%.0f",
                provider.run.id, work.id, work.kind, code, type(exc).__name__,
                safe_error_detail(exc), context.model_calls, context.tool_calls,
                (perf_counter() - started) * 1000)
