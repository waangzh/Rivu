import pytest

from app.services.export.models import ExportFinding
from app.services.export.pagination import paginate


def test_paginate_full_page():
    items = [ExportFinding(path=f"app/{index}.py", line=index, severity="low", title="t", detail="d")
             for index in range(3)]
    assert paginate(items, page=1, page_size=100) == items


def test_paginate_rejects_invalid_page():
    with pytest.raises(ValueError):
        paginate([], page=0, page_size=10)
