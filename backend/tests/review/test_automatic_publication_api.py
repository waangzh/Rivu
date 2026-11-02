import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.orm import WorkerJobOrm
from app.review.contracts import ReviewRequest
from app.services.review.task_queue import DatabaseTaskQueue
from tests.review import test_api
from tests.support.publication_fixtures import publish_config

service_pipeline = test_api.service_pipeline


def configure(monkeypatch, **updates):
    config = publish_config(rivu_github_publish_allowed_repositories="1", **updates)
    for name in type(config).model_fields:
        if name.startswith(("rivu_github_publish_", "rivu_publication_")):
            monkeypatch.setattr(settings, name, getattr(config, name))


@pytest.mark.parametrize("host,authorization,origin", [
    ("127.0.0.1", None, None),
    ("127.0.0.1", "Bearer wrong", None),
    ("192.0.2.1", "Bearer " + "x" * 32, None),
    ("127.0.0.1", "Bearer " + "x" * 32, "https://untrusted.example"),
])
async def test_auto_publish_requires_operator_before_preparation(
        service_pipeline, monkeypatch, host, authorization, origin):
    service, github, app = service_pipeline
    configure(monkeypatch)
    before = service.store.list_runs()["total"]
    headers = {"Idempotency-Key": "auto"}
    if authorization:
        headers["Authorization"] = authorization
    if origin:
        headers["Origin"] = origin
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1234)),
                                base_url="http://localhost") as client:
        response = await client.post("/api/reviews", headers=headers,
            json={"pr_url": github.pr.html_url, "auto_publish": True})
    assert response.status_code == 403
    assert github.calls == 0
    assert service.store.list_runs()["total"] == before


async def test_disabled_auto_publish_does_not_start_review(service_pipeline, monkeypatch):
    _, github, app = service_pipeline
    monkeypatch.setattr(settings, "rivu_github_publish_enabled", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                base_url="http://localhost") as client:
        response = await client.post("/api/reviews", headers={"Idempotency-Key": "disabled"},
            json={"pr_url": github.pr.html_url, "auto_publish": True})
    assert response.status_code == 503
    assert github.calls == 0


async def test_authorization_and_dispatch_are_durable_and_creation_is_idempotent(
        service_pipeline, monkeypatch):
    service, github, app = service_pipeline
    configure(monkeypatch)
    headers = {"Idempotency-Key": "authorized", "Authorization": "Bearer " + "x" * 32}
    body = {"pr_url": github.pr.html_url, "auto_publish": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                base_url="http://localhost") as client:
        response = await client.post("/api/reviews", headers=headers, json=body)
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        repeated = await client.post("/api/reviews", headers=headers, json=body)
        assert repeated.json()["id"] == run_id
        assert github.calls == 1
        conflict = await client.post("/api/reviews", headers=headers,
                                    json={**body, "auto_publish": False})
        assert conflict.status_code == 409
        status = await client.get(f"/api/reviews/{run_id}/automatic-publication")
        assert status.json()["state"] == "waiting_review"
    with service.store.sessions() as session:
        jobs = list(session.scalars(select(WorkerJobOrm).where(WorkerJobOrm.task_id == run_id)))
        assert sorted(job.kind for job in jobs) == ["automatic_publication", "review"]
        intent = next(job for job in jobs if job.kind == "automatic_publication")
        grant = intent.payload["authorization"]
        assert grant["review_id"] == run_id
        assert grant["repository_id"] == 1
        assert grant["base_sha"] == response.json()["snapshot"]["declared_base_sha"]
        assert grant["head_sha"] == response.json()["snapshot"]["head_sha"]
        assert grant["publisher_identity"] == "user:7"
        assert "x" * 32 not in json.dumps([job.payload for job in jobs])
        assert "github-fixture" not in response.text
    assert DatabaseTaskQueue(service.store.sessions).claim(
        worker_id="publisher", kinds=("automatic_publication",)) is None


async def test_auto_publish_cannot_be_requested_without_trusted_policy(service_pipeline):
    service, github, _ = service_pipeline
    with pytest.raises(ValueError, match="automatic_publication_not_authorized"):
        await service.create(ReviewRequest(pr_url=github.pr.html_url, auto_publish=True), "direct")
    assert github.calls == 0


async def test_auto_intent_and_review_rollback_together(service_pipeline, monkeypatch):
    service, github, app = service_pipeline
    configure(monkeypatch)
    before = service.store.list_runs()["total"]
    original = DatabaseTaskQueue.enqueue

    def fail_auto(self, **kwargs):
        if kwargs.get("kind") == "automatic_publication":
            raise RuntimeError("simulated database failure")
        return original(self, **kwargs)

    monkeypatch.setattr(DatabaseTaskQueue, "enqueue", fail_auto)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1), raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.post("/api/reviews",
            headers={"Idempotency-Key": "rollback", "Authorization": "Bearer " + "x" * 32},
            json={"pr_url": github.pr.html_url, "auto_publish": True})
    assert response.status_code == 500
    assert service.store.list_runs()["total"] == before


async def test_plain_reviews_and_preview_do_not_authorize_publication(service_pipeline, monkeypatch):
    service, github, app = service_pipeline
    configure(monkeypatch)
    before = service.store.list_runs()["total"]
    preview = await service.preview(ReviewRequest(pr_url=github.pr.html_url, auto_publish=True))
    assert preview["plan"].units
    assert service.store.list_runs()["total"] == before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://localhost") as client:
        response = await client.post("/api/reviews", headers={"Idempotency-Key": "plain"},
                                     json={"pr_url": github.pr.html_url})
        assert response.status_code == 202
        assert (await client.get(
            f"/api/reviews/{response.json()['id']}/automatic-publication")).json() is None


async def test_repository_outside_allowlist_never_persists_auto_intent(service_pipeline, monkeypatch):
    service, github, app = service_pipeline
    configure(monkeypatch)
    monkeypatch.setattr(settings, "rivu_github_publish_allowed_repositories", "999")
    before = service.store.list_runs()["total"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                base_url="http://localhost") as client:
        response = await client.post("/api/reviews",
            headers={"Idempotency-Key": "not-allowed", "Authorization": "Bearer " + "x" * 32},
            json={"pr_url": github.pr.html_url, "auto_publish": True})
    assert response.status_code == 403
    assert service.store.list_runs()["total"] == before
