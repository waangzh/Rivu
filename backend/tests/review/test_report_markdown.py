import json
from pathlib import Path

from app.services.review.report_markdown import render_report
from app.services.review.report_models import ReportFinding, ReportSection

FIXTURES = Path(__file__).parent / "fixtures"


def load_sections():
    payload = json.loads((FIXTURES / "report_findings.json").read_text(encoding="utf-8"))
    return [
        ReportSection(
            title=section["title"],
            findings=tuple(ReportFinding(**finding) for finding in section["findings"]),
        )
        for section in payload["sections"]
    ]


def test_render_report_contains_sections_and_rows():
    markdown = render_report(load_sections())
    assert "## 正确性" in markdown
    assert "| app/a.py | 10 | 高 | 缺少空值检查 |" in markdown
    assert "## 安全性" in markdown


def test_render_report_without_sections():
    assert render_report([]) == "暂无审查发现。"
