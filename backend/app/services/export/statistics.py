"""导出统计。"""

from datetime import datetime

from app.services.export.models import ExportFinding


def severity_counts(findings: list[ExportFinding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def duration_seconds(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    if not started_at or not completed_at:
        return None
    return (completed_at - started_at).seconds
