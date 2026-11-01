from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from tests.review.vnext_helpers import setup_run


async def test_progress_exposes_work_and_unknown_usage_before_report(tmp_path, monkeypatch):
    from app.api import reviews

    store, run, lease, _ = setup_run(tmp_path)
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        store=store, get_task=store.get))
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    before = store.get(run.id).updated_at
    store.reserve(run.id, work, 1000, lease)
    store.deliver(run.id, work, ["h1"], {}, lease)
    assert store.get(run.id).updated_at == before
    assert store.get(run.id).report is None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/reviews/{run.id}/progress")
        assert response.status_code == 200
        progress = response.json()
        assert progress["units"][0]["status"] == "running"
        assert progress["units"][0]["material_delivery"] == "complete"
        assert progress["usage"]["model_calls"] == 1
        assert progress["usage"]["unknown_calls"] == 1
        assert progress["usage"]["input_tokens"] is None
        assert progress["usage"]["cached_input_tokens"] is None
        assert progress["usage"]["committed_input_tokens"] == 1000

        store.finish_work(run.id, work, "completed", {}, None, lease, tool_calls=3)
        assert store.get(run.id).updated_at == before
        refreshed = (await client.get(f"/api/reviews/{run.id}/progress")).json()
        assert refreshed["units"][0]["status"] == "completed"
        assert refreshed["units"][0]["tool_calls"] == 3
        assert (await client.get("/api/reviews/missing/progress")).status_code == 404
