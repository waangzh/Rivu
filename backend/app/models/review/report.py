"""审查报告接口的响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.review.domain import Report


class ReviewReportResponse(BaseModel):
    report: Report | None = None
    markdown: str = ""
    finding_count: int = Field(default=0, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    generated_at: datetime | None = None
