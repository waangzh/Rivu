from datetime import datetime, timedelta

from app.services.review.stats import (
    summarize_duration,
    summarize_findings,
    weighted_score,
)


def test_summarize_findings_counts_by_severity():
    findings = [
        {"severity": "critical", "path": "app/a.py"},
        {"severity": "high", "path": "app/a.py"},
        {"severity": "low", "path": "app/b.py"},
    ]
    summary = summarize_findings(findings, ["app/a.py", "app/b.py"])
    assert summary["total"] == 3
    assert summary["by_severity"] == {
        "critical": 1,
        "high": 1,
        "medium": 0,
        "low": 1,
    }
    assert summary["average_per_file"] == 1.5
    assert summary["weighted_score"] == 9


def test_weighted_score_uses_default_weights():
    counts = {"critical": 1, "high": 0, "medium": 1, "low": 2}
    assert weighted_score(counts) == 9


def test_summarize_duration_ignores_incomplete_ranges():
    start = datetime(2026, 1, 1, 12, 0, 0)
    assert summarize_duration(start, None) is None
    assert summarize_duration(start, start + timedelta(minutes=2)) == 120
