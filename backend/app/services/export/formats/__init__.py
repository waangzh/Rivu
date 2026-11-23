"""导出格式渲染器。"""

from app.services.export.formats.csv_format import render_csv
from app.services.export.formats.json_format import render_json
from app.services.export.formats.markdown_format import render_markdown

RENDERERS = {"csv": render_csv, "json": render_json, "markdown": render_markdown}
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}
