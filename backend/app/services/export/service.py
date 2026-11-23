"""审查结果导出服务。"""

from datetime import datetime, timezone

from app.review.domain import Run
from app.services.export.filters import filter_findings, sort_findings
from app.services.export.formats import MEDIA_TYPES, RENDERERS
from app.services.export.models import ExportFinding, ExportRequest, ExportResult
from app.services.export.naming import export_filename
from app.services.export.pagination import paginate
from app.services.export.storage import ExportStorage


class ExportService:
    def __init__(self, storage: ExportStorage | None = None):
        self.storage = storage or ExportStorage()

    def _findings(self, task: Run) -> list[ExportFinding]:
        findings = []
        for finding in (task.report.findings if task.report else []):
            location = finding.location
            findings.append(ExportFinding(
                path=location.path if location else "unknown",
                line=location.line if location else 0,
                severity=finding.severity,
                title=finding.claim,
                detail=finding.impact,
                attribution=finding.attribution,
            ))
        return findings

    async def export(self, task: Run, request: ExportRequest) -> ExportResult:
        findings = sort_findings(filter_findings(self._findings(task), request.severities, request.paths))
        findings = paginate(findings, request.page, request.page_size)
        content = RENDERERS[request.format](findings)
        filename = export_filename(task.id, request.format, request.filename)
        self.storage.write(task.id, filename, content)
        return ExportResult(filename=filename, content=content, media_type=MEDIA_TYPES[request.format],
            size_bytes=len(content.encode("utf-8")), generated_at=datetime.now(timezone.utc))

    def read_export(self, task_id: str, filename: str) -> str:
        return self.storage.read(task_id, filename)
