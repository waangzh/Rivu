"""把审查发现渲染为 Markdown 报告。"""

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
}
DETAIL_LIMIT = 200


def render_finding(finding):
    detail = finding.detail[:DETAIL_LIMIT]
    return (
        f"| {finding.path} | {finding.line} | "
        f"{SEVERITY_LABELS.get(finding.severity, finding.severity)} | "
        f"{finding.title} | {detail} |"
    )


def render_section(section):
    lines = [
        f"## {section.title}",
        "",
        "| 文件 | 行 | 严重度 | 标题 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in sorted(section.findings, key=lambda item: f"{item.path}:{item.line}"):
        lines.append(render_finding(finding))
    return "\n".join(lines)


def render_report(sections):
    if not sections:
        return "暂无审查发现。"
    return "\n\n".join(render_section(section) for section in sections)
