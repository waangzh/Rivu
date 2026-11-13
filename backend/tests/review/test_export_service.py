import asyncio
from types import SimpleNamespace

from app.services.export.models import ExportRequest
from app.services.export.service import ExportService
from app.services.export.storage import ExportStorage


def finding(path, severity):
    return SimpleNamespace(location=SimpleNamespace(path=path, line=10), severity=severity,
        claim="缺少空值检查", impact="调用方可能传入 None", attribution="introduced")


def test_export_writes_markdown_and_returns_metadata(tmp_path):
    task = SimpleNamespace(id="task-1", report=SimpleNamespace(findings=[finding("app/a.py", "high")]))
    service = ExportService(ExportStorage(tmp_path))
    result = asyncio.run(service.export(task, ExportRequest(format="markdown")))
    assert result.filename.endswith(".markdown")
    assert result.size_bytes == len(result.content.encode("utf-8"))
    assert "缺少空值检查" in (tmp_path / "task-1" / result.filename).read_text(encoding="utf-8")


def test_export_applies_severity_filter(tmp_path):
    task = SimpleNamespace(id="task-1", report=SimpleNamespace(findings=[
        finding("app/a.py", "high"), finding("app/b.py", "low")]))
    service = ExportService(ExportStorage(tmp_path))
    result = asyncio.run(service.export(task, ExportRequest(format="json", severities=("low",))))
    assert "app/b.py" in result.content
    assert "app/a.py" not in result.content
