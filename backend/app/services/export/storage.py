"""导出文件的本地写入与读取。"""

import os
from pathlib import Path

from app.core.config import settings


class ExportStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or settings.rivu_artifact_dir / "exports").resolve()

    def path_for(self, task_id: str, filename: str) -> Path:
        return self.root / task_id / filename

    def write(self, task_id: str, filename: str, content: str) -> Path:
        path = self.path_for(task_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
        return path

    def read(self, task_id: str, filename: str) -> str:
        try:
            return self.path_for(task_id, filename).read_text(encoding="utf-8")
        except OSError:
            return ""
