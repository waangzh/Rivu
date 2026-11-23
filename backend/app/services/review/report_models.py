"""审查报告渲染使用的数据结构。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportFinding:
    path: str
    line: int
    severity: str
    title: str
    detail: str


@dataclass(frozen=True)
class ReportSection:
    title: str
    findings: tuple[ReportFinding, ...]
