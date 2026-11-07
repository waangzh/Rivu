"""Create once, inspect, stop and retry within the original resource boundary."""

import asyncio

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.api import reviews as _reviews
from app.review.contracts import ReviewRequest
from app.llm.providers import LLMProviderError, model_error_message
from app.review.domain import Run
from app.core.config import settings
from app.models.publication import AutomaticPublicationStatus
from app.services.publication.automatic_publication import (
    AutomaticPublicationPolicy, automatic_publication_status,
)
from app.tools.git_tool import GitToolError
from app.tools.github.github_publisher import GitHubPublishError
from app.services.workspaces.local_inputs import LocalInputError

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("", response_model=Run, status_code=202)
async def create_review(request: ReviewRequest, http_request: Request,
                        idempotency_key: str = Header(min_length=1, max_length=200),
                        authorization: str | None = Header(default=None)):
    try:
        policy = None
        if request.auto_publish:
            from app.api.publications import require_operator
            require_operator(http_request, authorization)
            policy = AutomaticPublicationPolicy.from_config(settings)
        return await _reviews.get_review_service().create(
            request, idempotency_key, automatic_policy=policy)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except GitToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except GitHubPublishError as exc:
        raise HTTPException(403, {"code": exc.code}) from None
    except LocalInputError as exc:
        raise HTTPException(422, {"code": str(exc), "message": "本地快照准备失败，请检查仓库注册、提交版本、对象完整性与输入大小限制。", "retryable": False}) from None
    except LLMProviderError as exc:
        raise HTTPException(422, {"code": str(exc), "message": model_error_message(str(exc)),
                                  "retryable": False}) from None
    except ValueError as exc:
        code = str(exc) if str(exc) in {"idempotency_key_conflict", "review_deleted"} else "review_request_invalid"
        raise HTTPException(410 if code == "review_deleted" else 409, {"code": code}) from None


@router.get("")
async def list_reviews(task_status: str | None = Query(None, alias="status"),
        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, max_length=200), repository: str | None = None,
        pr_number: int | None = Query(None, ge=1), head_sha: str | None = None):
    return await asyncio.to_thread(_reviews.get_review_service().list_tasks, status=task_status,
        page=page, page_size=page_size, q=q, repository=repository, pr_number=pr_number, head_sha=head_sha)


@router.get("/{task_id}", response_model=Run)
async def get_review(task_id: str):
    run = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if run is None:
        raise HTTPException(404, "Review not found")
    return run


@router.get("/{task_id}/automatic-publication", response_model=AutomaticPublicationStatus | None)
async def get_automatic_publication(task_id: str):
    await get_review(task_id)
    return await asyncio.to_thread(automatic_publication_status,
        _reviews.get_review_service().store.sessions, task_id,
        enabled=settings.rivu_github_publish_enabled)


@router.get("/{task_id}/units")
async def get_units(task_id: str):
    await get_review(task_id)
    return await asyncio.to_thread(_reviews.get_review_service().store.work_items, task_id)


@router.get("/{task_id}/progress")
async def get_progress(task_id: str):
    await get_review(task_id)
    store = _reviews.get_review_service().store
    units, usage = await asyncio.gather(
        asyncio.to_thread(store.work_items, task_id),
        asyncio.to_thread(store.usage, task_id),
    )
    return {"units": units, "usage": usage}


@router.delete("/{task_id}", status_code=204)
async def delete_review(task_id: str):
    try:
        deleted = await asyncio.to_thread(_reviews.get_review_service().delete_task, task_id)
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None
    if not deleted:
        raise HTTPException(404, "Review not found")
    return Response(status_code=204)


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_review(task_id: str):
    await get_review(task_id)
    if not await _reviews.get_review_service().cancel_task(task_id):
        raise HTTPException(409, "Review already finished")
    return {"review_id": task_id, "status": "cancelled"}


async def _retry(task_id, overview):
    try:
        return await _reviews.get_review_service().retry(task_id, overview=overview)
    except KeyError:
        raise HTTPException(404, "Review not found") from None
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None


@router.post("/{task_id}/retry", response_model=Run, status_code=202)
async def retry_review(task_id: str):
    return await _retry(task_id, False)


@router.post("/{task_id}/overview/retry", response_model=Run, status_code=202)
async def retry_overview(task_id: str):
    return await _retry(task_id, True)
