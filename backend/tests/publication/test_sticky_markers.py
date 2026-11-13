from types import SimpleNamespace

from app.services.review.sticky import controlled_body, summary_marker, summary_metadata


def context():
    return {"repository_id": 7, "pr_number": 12, "publisher_identity": "bot", "order": "1",
            "owner": "o", "repo": "r", "publisher_id": 9}


def test_marker_round_trip():
    ctx = context()
    attempt = SimpleNamespace(id="a1", review_id="r1", head_sha="a" * 40)
    body = controlled_body("hello", attempt, ctx)
    assert summary_marker(ctx) in body
    metadata = summary_metadata(body, ctx)
    assert metadata["head_sha"] == "a" * 40
    assert metadata["order"] == "1"
