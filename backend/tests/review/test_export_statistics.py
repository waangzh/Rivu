from datetime import datetime, timedelta

from app.services.export.models import ExportFinding
from app.services.export.statistics import duration_seconds, severity_counts


def test_severity_counts():
    items = [ExportFinding(path="a", line=1, severity="high", title="t", detail="d"),
             ExportFinding(path="b", line=2, severity="high", title="t", detail="d"),
             ExportFinding(path="c", line=3, severity="low", title="t", detail="d")]
    assert severity_counts(items) == {"high": 2, "low": 1}


def test_duration_seconds_for_minutes():
    start = datetime(2026, 1, 1, 12, 0, 0)
    assert duration_seconds(start, start + timedelta(minutes=2)) == 120
    assert duration_seconds(start, None) is None
