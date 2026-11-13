"""Markdown 导出。"""

from app.services.export.models import ExportFinding


def _cell(value) -> str:
    return str(value).replace("\n", " ")


def render_markdown(findings: list[ExportFinding]) -> str:
    lines = ["# Rivu 审查发现", "", "| 文件 | 行 | 严重度 | 标题 | 说明 |", "| --- | --- | --- | --- | --- |"]
    for finding in findings:
        lines.append(f"| {_cell(finding.path)} | {finding.line} | {_cell(finding.severity)} | "
                     f"{_cell(finding.title)} | {_cell(finding.detail)} |")
    if not findings:
        lines.append("| - | - | - | 暂无发现 | - |")
    return "\n".join(lines) + "\n"
