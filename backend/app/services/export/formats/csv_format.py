"""CSV 导出。"""

import csv
import io

from app.services.export.models import ExportFinding

HEADER = ("path", "line", "severity", "title", "detail", "attribution")


def render_csv(findings: list[ExportFinding]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    for finding in findings:
        writer.writerow((finding.path, finding.line, finding.severity, finding.title,
                         finding.detail, finding.attribution))
    return buffer.getvalue()
