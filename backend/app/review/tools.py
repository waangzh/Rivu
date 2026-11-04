"""The entire agent tool surface; runtime authority is never a model argument."""

import asyncio
import json
import logging
from inspect import signature
from functools import wraps
from typing import Annotated, Literal

from langchain.tools import ToolRuntime, tool
from pydantic import Field

from app.review.context import AgentContext, InvestigationState, TOOL_LOOP_WARNING
from app.review.domain import CandidateInput
from app.review.findings import admit_candidate, candidate_summary
from app.review.retrieval import find_paths, search
from app.review.evidence import SourceReadError
from app.review.observability import event, stage

logger = logging.getLogger("Rivu.Review.Tools")


def bounded(function):
    parameters = signature(function)
    @wraps(function)
    async def execute(*args, **kwargs):
        context = kwargs["runtime"].context
        await context.check_active()
        context.tool_calls += 1
        bound = parameters.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = {key: value for key, value in bound.arguments.items() if key != "runtime"}
        if context.investigation_closed and function.__name__ != "submit_candidate":
            event("tool.rejected", tool=function.__name__, reason="investigation_closed", ok=False)
            result = {"ok": False, "error": "investigation_closed",
                      "next_action": "Return the required result tool using existing evidence; "
                      "state a concrete missing premise if the investigation is unfinished."}
            context.note_tool_result(function.__name__, arguments, result)
            return result
        if context.tool_calls > context.provider.run.configuration.options.limits.tool_calls:
            event("tool.rejected", tool=function.__name__, reason="tool_call_limit", ok=False)
            logger.warning("工具调用被预算阻止 review_id=%.8s work_id=%.8s tool=%s reason=tool_call_limit",
                           context.provider.run.id, context.work.id, function.__name__)
            return {"ok": False, "error": "tool_call_limit"}
        if (context.window and context.window.checkpoint_pending
                and function.__name__ not in {"record_investigation", "submit_candidate"}):
            event("tool.rejected", tool=function.__name__, reason="context_checkpoint_required", ok=False)
            result = {"ok": False, "error": "context_checkpoint_required",
                      "next_action": "Record current questions, conclusions, counterevidence and "
                                     "missing premises before continuing, or return the final result."}
            context.note_tool_result(function.__name__, arguments, result)
            return result
        try:
            with stage("tool.execute", run_type="tool", tool=function.__name__) as observation:
                observation.content(inputs=arguments)
                result = await function(*args, **kwargs)
                partial = result.get("partial")
                if (result.get("scope_complete") is False or result.get("has_more") is True
                        or result.get("truncated") is True):
                    partial = True
                observation.set(ok=result.get("ok") is True,
                                source_count=len(context.visible) + len(context.pending),
                                truncated=result.get("truncated"), partial=partial,
                                scope_complete=result.get("scope_complete"),
                                has_more=result.get("has_more"),
                                **{f"{name}_count": len(result[plural])
                                   for name, plural in (("match", "matches"), ("path", "paths"),
                                                        ("edge", "edges"))
                                   if isinstance(result.get(plural), list)})
                observation.content(outputs=result)
        except SourceReadError as exc:
            event("tool.rejected", tool=function.__name__, reason=exc.code, ok=False)
            logger.warning("工具调用被拒绝 review_id=%.8s work_id=%.8s tool=%s reason=%s",
                           context.provider.run.id, context.work.id, function.__name__, exc.code)
            result = {"ok": False, "error": exc.code, **exc.details}
        except (ValueError, OSError) as exc:
            reason = str(exc) if type(exc) is ValueError and str(exc) in {
                "source_not_visible_in_this_attempt", "candidate_snapshot_mismatch",
                "candidate_location_not_sourced", "investigation_reference_not_received",
                "root_cause_not_sourced",
            } else "source_or_submission_rejected"
            event("tool.rejected", tool=function.__name__,
                  reason=reason, ok=False)
            logger.warning("工具调用被拒绝 review_id=%.8s work_id=%.8s tool=%s error_type=%s",
                           context.provider.run.id, context.work.id, function.__name__, type(exc).__name__)
            result = {"ok": False, "error": reason}
            if reason in {"source_not_visible_in_this_attempt", "candidate_location_not_sourced",
                          "root_cause_not_sourced"}:
                result.update(
                    available_source_refs=list(context.visible)[:64],
                    next_action="Use received source_ref handles covering the proposed location. "
                                "Never invent handles or reuse another attempt's handles.")
        await context.check_active()
        # Submission receipts remain idempotent; never block saving a candidate.
        if function.__name__ != "submit_candidate":
            repetition = context.note_tool_result(function.__name__, arguments, result)
            if repetition == TOOL_LOOP_WARNING:
                return {**result, "progress_warning": "Identical request returned the same result. "
                        "Reuse that result or change the request to resolve a missing premise. "
                        "This does not determine whether the investigation is complete."}
        logger.debug("工具调用完成 review_id=%.8s work_id=%.8s tool=%s",
                     context.provider.run.id, context.work.id, function.__name__)
        return result
    return execute


@tool
@bounded
async def read_file(path: str, revision: Literal["base", "head"],
                    start: Annotated[int, Field(ge=1, strict=True)],
                    end: Annotated[int, Field(ge=1, strict=True)],
                    runtime: ToolRuntime[AgentContext]) -> dict:
    """Read fixed source lines using start/end, not start_line/end_line.

    Lines are 1-based inclusive: at most 240, so end <= start + 239.
    Reuse returned total_lines/max_read_lines for later ranges. For a named
    function, prefer read_symbol. Rejected ranges include corrected arguments.
    """
    return await runtime.context.read(path, start, end, revision)


@tool
@bounded
async def read_diff(hunk_id: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Deliver a missing required hunk member, using an ID from the frozen unit."""
    unit = next(unit for unit in runtime.context.provider.run.plan.units
                if unit.id == runtime.context.work.unit_id)
    if hunk_id not in unit.required_hunk_ids:
        raise ValueError("hunk_not_in_unit")
    return await runtime.context.provider.material(runtime.context, hunk_id)


@tool
@bounded
async def search_code(query: str, runtime: ToolRuntime[AgentContext], path: str = "",
                      glob: str = "", revision: Literal["base", "head"] = "head",
                      cursor: str = "",
                      limit: Annotated[int, Field(ge=1, le=40, strict=True)] = 12,
                      exclude_globs: Annotated[list[str] | None, Field(max_length=16)] = None) -> dict:
    """Bounded literal search. Results are hints, read source before citing them.

    Glob * and ? stay within one path component; ** spans directories.
    Patterns with / are repository-relative; bare *.ts matches basenames anywhere.
    Exclude tests with exclude_globs=["**/__tests__/**"], never a leading !.
    limit is 1..40. Continue next_cursor with identical query/path/glob/revision
    and exclude_globs; omit cursor if filters change. An empty partial page is not absence.
    """
    return await search(runtime.context, query, path, glob, revision, cursor, limit,
                        exclude_globs=exclude_globs)


@tool
@bounded
async def find_files(runtime: ToolRuntime[AgentContext], query: str = "", path: str = "",
                     glob: str = "", cursor: str = "",
                     limit: Annotated[int, Field(ge=1, le=80, strict=True)] = 80,
                     exclude_globs: Annotated[list[str] | None, Field(max_length=16)] = None) -> dict:
    """Page tracked paths, limit 1..80, with the same glob rules as search_code.

    Use exclude_globs for exclusions, never !. Keep all filters for next_cursor.
    scope_empty means no authorized paths satisfy the filters.
    """
    return await asyncio.to_thread(find_paths, runtime.context, query, path, glob, cursor, limit,
                                   exclude_globs=exclude_globs)


@tool
@bounded
async def submit_candidate(proposal: CandidateInput, runtime: ToolRuntime[AgentContext]) -> dict:
    """Persist a concrete defect claim for independent verification immediately.

    Success means saved, never confirmed. Cite only source_ref handles actually received.
    Check relevant guards and counterevidence first. Do not submit a hypothesis already
    refuted by source; reopen it only for new evidence or a specific reasoning error.
    """
    context = runtime.context
    candidate = admit_candidate(proposal, context, runtime.tool_call_id)
    candidate_id = await asyncio.to_thread(context.provider.store.submit,
        context.provider.run.id, context.work, candidate, context.provider.lease)
    event("candidate.submitted", candidate_id=candidate_id, status="unverified",
          source_count=len(candidate.sources))
    context.submitted[candidate_id] = {**candidate_summary(candidate), "id": candidate_id,
                                     "source_refs": proposal.source_refs}
    context.note_tool_result("submit_candidate", {}, {"ok": True, "candidate_id": candidate_id})
    logger.info("候选已保存并排队核验 review_id=%.8s work_id=%.8s candidate_id=%.8s",
                context.provider.run.id, context.work.id, candidate_id)
    saved = await asyncio.to_thread(context.provider.store.candidates, context.provider.run.id)
    known = [candidate_summary(item) for item in saved if item.id != candidate_id][-8:]
    context.submitted.update({item["id"]: item for item in known})
    return {"ok": True, "candidate_id": candidate_id, "status": "unverified",
            "already_submitted": known}


@tool
@bounded
async def record_investigation(note: InvestigationState, runtime: ToolRuntime[AgentContext]) -> dict:
    """Save or replace a question's compact notes, not a finding or completion verdict.

    Record supporting source_refs, counterevidence_refs, bounded retrieval_refs and scope.
    depends_on links prerequisite question IDs; outcome is separate from progress status.
    Notes guide batched context compaction. Open/blocked questions keep their premises.
    Use at branch changes or a context checkpoint, preferably alongside the next read.
    Resolve source-refuted branches with outcome=refuted and their counterevidence.
    Reuse question_id when new evidence or a reasoning correction reopens a branch.
    Submit supported defects separately with submit_candidate.
    """
    context = runtime.context
    context.resolve(list(set(note.source_refs + note.counterevidence_refs)))
    if (not set(note.retrieval_refs) <= context.visible_receipts
            or not set(note.candidate_ids) <= context.submitted.keys()):
        raise ValueError("investigation_reference_not_received")
    context.investigations[note.question_id] = note
    context.memory_revision += 1
    return {"ok": True, "investigation": note.model_dump()}


@tool
@bounded
async def recall_context(receipt_ref: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Restore an archived tool receipt received in this attempt, using its R handle.

    Preserves exact query/snapshot/scope and pagination. Search and relation results
    remain hints, never source evidence. Use read_file or read_diff for source bytes.
    """
    return await runtime.context.recall(receipt_ref)


@tool
@bounded
async def read_discussion(index: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read frozen untrusted discussion background; it is never source evidence."""
    context = runtime.context
    if not context.provider.run.configuration.options.include_pr_discussions:
        raise ValueError("discussion_disabled")
    if index < 0 or index >= len(context.provider.discussions):
        raise ValueError("discussion_not_found")
    text = json.dumps(context.provider.discussions[index], ensure_ascii=False)
    if len(text) > 12000:
        raise ValueError("discussion_too_large")
    return {"source": "untrusted_discussion", "content": text}


@tool
@bounded
async def read_symbol(path: str, symbol: str, runtime: ToolRuntime[AgentContext],
                      revision: Literal["base", "head"] = "head") -> dict:
    """Parse one fixed file on demand and read a unique symbol's complete source."""
    context = runtime.context
    found = await context.provider.index.symbol(path, symbol, revision)
    return await context.read(path, found["start_line"], found["end_line"], revision)


@tool
@bounded
async def find_relations(path: str, symbol: str, runtime: ToolRuntime[AgentContext],
                         direction: Literal["in", "out", "both"] = "both", scope: str = "") -> dict:
    """Find bounded caller/callee/reference hints, parsing likely related files only."""
    return await runtime.context.provider.index.relations(path, symbol, direction, scope)


READ_TOOLS = [read_file, read_diff, search_code, find_files, read_symbol, find_relations]
