"""导出文件名生成与基础清理。"""

import re
from datetime import datetime

_UNSAFE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name.strip())
    return cleaned or "review-export"


def export_filename(task_id: str, format: str, requested: str | None = None) -> str:
    if requested:
        return sanitize_filename(requested)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"rivu-{task_id[:8]}-{stamp}.{format}"
