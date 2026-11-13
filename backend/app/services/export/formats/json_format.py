"""JSON 导出。"""

import json

from app.services.export.models import ExportFinding


def render_json(findings: list[ExportFinding]) -> str:
    return json.dumps([finding.__dict__ for finding in findings], ensure_ascii=False,
                      indent=2, default=str)
