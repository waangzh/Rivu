"""导出结果分页。"""

from app.services.export.models import ExportFinding


def paginate(findings: list[ExportFinding], page: int = 1, page_size: int = 100) -> list[ExportFinding]:
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    start = (page - 1) * page_size
    end = start + page_size - 1
    return findings[start:end]
