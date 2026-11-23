import json

from app.services.export.formats import MEDIA_TYPES, RENDERERS
from app.services.export.models import ExportFinding


def findings():
    return [
        ExportFinding(path="app/a.py", line=10, severity="high", title="缺少空值检查",
                      detail="调用方可能传入 None"),
        ExportFinding(path="app/b.py", line=3, severity="critical", title="路径拼接未校验",
                      detail="用户输入直接参与路径拼接"),
    ]


def test_csv_renderer_has_header_and_rows():
    content = RENDERERS["csv"](findings())
    assert content.splitlines()[0] == "path,line,severity,title,detail,attribution"
    assert "app/a.py" in content


def test_json_renderer_round_trips():
    payload = json.loads(RENDERERS["json"](findings()))
    assert payload[0]["path"] == "app/a.py"
    assert payload[1]["severity"] == "critical"


def test_markdown_renderer_has_table():
    content = RENDERERS["markdown"](findings())
    assert "| app/a.py | 10 | high |" in content


def test_media_types_cover_all_renderers():
    assert set(MEDIA_TYPES) == set(RENDERERS)
