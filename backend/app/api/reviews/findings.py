"""Machine findings for a review."""

import asyncio

from fastapi import APIRouter

from app.api import reviews as _reviews
from app.api.reviews.tasks import get_review

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/findings")
async def get_findings(task_id: str):
    run = await get_review(task_id)
    candidates = await asyncio.to_thread(_reviews.get_review_service().store.candidates, task_id)
    from app.review.findings import aggregate, group_findings
    return {"findings": run.report.findings if run.report else aggregate(candidates),
            "finding_groups": run.report.finding_groups if run.report else group_findings(candidates),
            "candidates": candidates, "report_version": run.report.version if run.report else None}
