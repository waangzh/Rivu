"""审查结果导出 API。"""

import asyncio

from fastapi import APIRouter, HTTPException, Response

from app.api import reviews as _reviews
from app.review.domain import TERMINAL
from app.services.export.models import ExportRequest
from app.services.export.service import ExportService

router = APIRouter(prefix="/reviews", tags=["reviews"])
_export_service = ExportService()


@router.post("/{task_id}/export")
async def export_review(task_id: str, request: ExportRequest):
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in TERMINAL:
        raise HTTPException(status_code=409, detail="Review is not finished")
    result = await _export_service.export(task, request)
    return Response(content=result.content, media_type=result.media_type,
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'})


@router.get("/{task_id}/export/{filename}")
async def download_export(task_id: str, filename: str):
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    content = _export_service.read_export(task_id, filename)
    return Response(content=content, media_type="text/plain; charset=utf-8")
