"""审查结果导出的数据模型。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ExportFormat = Literal["csv", "json", "markdown"]


class ExportRequest(BaseModel):
    format: ExportFormat = "markdown"
    filename: str | None = None
    severities: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)


@dataclass(frozen=True)
class ExportFinding:
    path: str
    line: int
    severity: str
    title: str
    detail: str
    attribution: str = "introduced"


@dataclass(frozen=True)
class ExportResult:
    filename: str
    content: str
    media_type: str
    size_bytes: int
    generated_at: datetime
