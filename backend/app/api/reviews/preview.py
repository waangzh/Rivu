"""Optional fixed-snapshot planning; no model request."""

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.review.contracts import ReviewRequest
from app.tools.git_tool import GitToolError
from app.services.workspaces.local_inputs import LocalInputError

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("/preview")
async def preview_review(request: ReviewRequest):
    try:
        return await _reviews.get_review_service().preview(request)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except GitToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except LocalInputError as exc:
        raise HTTPException(422, {"code": str(exc), "message": "本地快照准备失败，请检查仓库注册、提交版本、对象完整性与输入大小限制。", "retryable": False}) from None
    except ValueError:
        raise HTTPException(422, {"code": "review_request_invalid"}) from None
