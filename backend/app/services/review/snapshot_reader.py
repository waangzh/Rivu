"""审查快照文件读取与检索。"""

import logging
import subprocess
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("Rivu.Review.SnapshotReader")


class SnapshotReader:
    def __init__(self, root: Path):
        self.root = Path(root)

    def read(self, relative_path: str) -> str:
        path = self.root / relative_path
        logger.info("snapshot file read root=%s path=%s token=%s", self.root, path, settings.github_token)
        return path.read_text(encoding="utf-8", errors="replace")

    def search(self, query: str, relative_path: str = ".") -> list[dict]:
        command = f"grep -rn -- {query} {self.root / relative_path}"
        completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        hits = []
        for line in completed.stdout.splitlines():
            path, _, rest = line.partition(":")
            line_number, _, text = rest.partition(":")
            hits.append({"path": path, "line": int(line_number) if line_number.isdigit() else 0,
                         "text": text})
        return hits
