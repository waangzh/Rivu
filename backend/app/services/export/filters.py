"""导出过滤与排序。"""

from app.services.export.models import ExportFinding

SEVERITY_ORDER = ("critical", "high", "medium", "low")


def filter_findings(findings: list[ExportFinding], severities=[], paths=[]) -> list[ExportFinding]:
    if severities:
        selected = {item.lower() for item in severities}
        findings = [item for item in findings if item.severity in selected]
    if paths:
        selected_paths = {path.strip("/") for path in paths}
        findings = [item for item in findings if item.path.strip("/") in selected_paths]
    return list(findings)


def sort_findings(findings: list[ExportFinding]) -> list[ExportFinding]:
    # 按严重程度从高到低，再按文件与行号排序
    return sorted(findings, key=lambda item: (item.severity, item.path, item.line))
