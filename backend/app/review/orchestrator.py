"""Finite work scheduling inside one existing WorkerJob lease."""

import asyncio
import logging
from datetime import timedelta
from time import perf_counter

from app.review.budget import BudgetGate
from app.review.deduplication import apply_deduplication, deduplicate_findings
from app.review.domain import utcnow
from app.review.observability import event, review_trace, stage
from app.review.report import build_report, generate_overview
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped

logger = logging.getLogger("Rivu.Review")


def discovery_priority(unit, index):
    """Keep risk ordering, but put executable changes ahead of supporting material."""
    from app.review.risk import DOCUMENTATION_SUFFIXES, RISK_PATH_GROUPS
    import re
    parts = set(part for path in unit.paths for part in re.split(r"[^a-z0-9]+", path.lower()))
    sensitive = bool(parts & set().union(*(RISK_PATH_GROUPS[key] for key in
        ("auth", "crypto", "money", "data", "concurrency"))))
    supporting = all(path.endswith(DOCUMENTATION_SUFFIXES) or any(part in path.lower().split("/")
        for part in ("test", "tests", "__tests__", ".changeset")) for path in unit.paths)
    return (supporting, not sensitive, {"high": 0, "medium": 1, "low": 2}[unit.risk], index)


class ReviewOrchestrator:
    def __init__(self, store, runner):
        self.store, self.runner = store, runner

    async def run(self, run_id, provider, lease):
        with review_trace(provider.run, lease) as observation:
            report = await self._run(run_id, provider, lease)
            observation.set(status=report.status)
            return report

    async def _run(self, run_id, provider, lease):
        started = perf_counter()
        run = await asyncio.to_thread(self.store.begin, run_id, lease)
        provider.run = run
        budget = BudgetGate(self.store, run, lease)
        running = {}
        last_dispatched = None
        limits = run.configuration.options.limits
        logger.info(
            "审查开始 review_id=%.8s units=%d concurrency=%d agent_call_limit=%d "
            "total_call_limit=%s input_token_limit=%s review_cutoff=%s hard_deadline=%s",
            run_id, len(run.plan.units), limits.concurrency, limits.model_calls, limits.total_model_calls,
            limits.total_input_tokens, run.review_cutoff.isoformat(), run.hard_deadline.isoformat())
        try:
            while True:
                await asyncio.to_thread(self.store.assert_active, run_id, lease)
                work_items = await asyncio.to_thread(self.store.work_items, run_id)
                queued = [item for item in work_items if item.status == "queued"]
                if utcnow() >= run.hard_deadline:
                    logger.warning("审查到达硬截止时间 review_id=%.8s active_agents=%d",
                                   run_id, len(running))
                    for task in running:
                        task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    running.clear()
                    for item in work_items:
                        if item.status in {"queued", "running"}:
                            await asyncio.to_thread(self.store.skip, run_id, item.id, "hard_deadline", lease)
                            event("work.skipped", work_id=item.id, kind=item.kind, reason="hard_deadline")
                    break
                for item in list(queued):
                    if item.kind in {"review", "supplement"} and utcnow() >= run.review_cutoff:
                        await asyncio.to_thread(self.store.skip, run_id, item.id, "review_cutoff", lease)
                        event("work.skipped", work_id=item.id, kind=item.kind, reason="review_cutoff")
                        logger.warning("工作项未启动 review_id=%.8s work_id=%.8s kind=%s reason=review_cutoff",
                                       run_id, item.id, item.kind)
                        queued.remove(item)
                priority = {unit.id: discovery_priority(unit, index)
                            for index, unit in enumerate(run.plan.units)}
                queued.sort(key=lambda item: priority[item.unit_id])
                while queued and len(running) < run.configuration.options.limits.concurrency:
                    discoveries = [item for item in queued if item.kind == "review"]
                    checks = [item for item in queued if item.kind == "verification"]
                    # Retain a discovery slot while verifiers are busy, including concurrency=1.
                    active_discovery = any(item.kind == "review" for item in running.values())
                    discovery_turn = (not active_discovery and (limits.concurrency > 1
                        or last_dispatched != "review" or not checks))
                    pool = discoveries if discoveries and discovery_turn else checks or discoveries or queued
                    item = pool[0]
                    queued.remove(item)
                    investigation_deadline = None
                    if item.kind == "review" and discoveries:
                        waves = max(1, (len(discoveries) + limits.concurrency - 1) // limits.concurrency)
                        seconds = max(0, (run.review_cutoff - utcnow()).total_seconds())
                        # A bounded first pass prevents one unit consuming every dispatch wave.
                        investigation_deadline = min(run.review_cutoff,
                            utcnow() + timedelta(seconds=max(180, seconds / waves)))
                    try:
                        claimed = await asyncio.to_thread(
                            self.store.claim, run_id, item.id, lease, check_budget=True,
                            investigation_deadline=investigation_deadline)
                        reason = None
                    except BudgetExceeded as exc:
                        reason = str(exc)
                    if reason:
                        if not running:
                            await asyncio.to_thread(self.store.skip, run_id, item.id, reason, lease)
                            event("work.skipped", work_id=item.id, kind=item.kind, reason=reason)
                            logger.warning("工作项未启动 review_id=%.8s work_id=%.8s kind=%s reason=%s",
                                           run_id, item.id, item.kind, reason)
                        continue
                    if claimed:
                        last_dispatched = claimed.kind
                        task = asyncio.create_task(self.runner.run(claimed, provider, budget))
                        running[task] = claimed
                if not running:
                    if await asyncio.to_thread(self.store.enqueue_gap_followups, run_id, lease):
                        continue
                    break
                done, _ = await asyncio.wait(running, timeout=min(0.2,
                    max(0, (run.hard_deadline - utcnow()).total_seconds())),
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    running.pop(task)
                    task.result()
        except (LeaseLost, ReviewStopped, asyncio.CancelledError):
            raise
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        await asyncio.to_thread(self.store.assert_active, run_id, lease)
        with stage("review.report") as observation:
            report = await asyncio.to_thread(build_report, run,
                await asyncio.to_thread(self.store.work_items, run_id),
                await asyncio.to_thread(self.store.candidates, run_id),
                await asyncio.to_thread(self.store.usage, run_id))
            observation.set(status=report.status, candidate_count=len(report.candidates),
                            finding_count=len(report.findings), gap_count=len(report.reasons))
        await asyncio.to_thread(self.store.save_report, run_id, report, lease)
        logger.info("审查事实已保存 review_id=%.8s status=%s candidates=%d findings=%d gaps=%d",
                    run_id, report.status, len(report.candidates), len(report.findings), len(report.reasons))
        deduplication = await deduplicate_findings(report, provider, budget, self.runner.model)
        await asyncio.to_thread(apply_deduplication, report, deduplication)
        report.usage = await asyncio.to_thread(self.store.usage, run_id)
        await asyncio.to_thread(self.store.save_report, run_id, report, lease)
        report.overview = await generate_overview(report, provider, budget, self.runner.model)
        report.usage = await asyncio.to_thread(self.store.usage, run_id)
        # Content version binds facts and overview; counters do not mutate confirmed text.
        from app.review.planner import identity
        report.version = identity([report.version, report.overview.model_dump()])
        await asyncio.to_thread(self.store.save_report, run_id, report, lease, finished=True)
        event("review.finished", status=report.status, candidate_count=len(report.candidates),
              finding_count=len(report.findings), gap_count=len(report.reasons),
              unknown_calls=report.usage["unknown_calls"])
        logger.info(
            "审查结束 review_id=%.8s status=%s overview=%s candidates=%d findings=%d gaps=%d "
            "model_calls=%s input_tokens=%s output_tokens=%s unknown_calls=%s elapsed_ms=%.0f",
            run_id, report.status, report.overview.status, len(report.candidates), len(report.findings),
            len(report.reasons), report.usage["model_calls"], report.usage["input_tokens"],
            report.usage["output_tokens"], report.usage["unknown_calls"], (perf_counter() - started) * 1000)
        return report
