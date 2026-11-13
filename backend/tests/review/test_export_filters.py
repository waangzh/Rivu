from app.services.export.filters import filter_findings, sort_findings
from app.services.export.models import ExportFinding


def finding(path, severity, line=1):
    return ExportFinding(path=path, line=line, severity=severity, title="t", detail="d")


def test_filter_findings_by_severity_and_path():
    items = [finding("app/a.py", "critical"), finding("app/b.py", "low"), finding("app/a.py", "medium", 5)]
    assert [item.severity for item in filter_findings(items, ["critical"])] == ["critical"]
    assert [item.line for item in filter_findings(items, paths=["app/a.py"])] == [1, 5]


def test_sort_findings_keeps_critical_first():
    items = [finding("app/a.py", "low"), finding("app/b.py", "critical"), finding("app/c.py", "medium")]
    assert sort_findings(items)[0].severity == "critical"
