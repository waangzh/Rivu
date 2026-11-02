import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, get_ident

import pytest
from sqlalchemy import event, select, update

from app.models.orm import PublicationAttemptOrm, ReviewTaskOrm, WorkerJobOrm
from app.review.domain import utcnow
from app.services.publication.automatic_publication import (
    AutomaticPublicationPolicy, automatic_publication_status,
)
from app.services.publication.publication_repository import PublicationLeaseLost
from app.services.review.task_queue import DatabaseTaskQueue
from tests.publication.test_outbox import setup


async def setup_automatic(publication_pipeline, *, grant_update=None):
    task, api, service, store = await setup(publication_pipeline)
    grant = AutomaticPublicationPolicy.from_config(service.config).authorize(task)
    if grant_update:
        grant = grant.model_copy(update=grant_update)
    queue = DatabaseTaskQueue(store.sessions)
    job_id = queue.enqueue(task_id=task.id, kind="automatic_publication",
        idempotency_key=f"automatic_publication:{task.id}",
        payload={"authorization": grant.model_dump(mode="json"), "state": "waiting_review"})
    return task, api, service, store, job_id


def status(service, task):
    return automatic_publication_status(service.repo.sessions, task.id, enabled=True)


def persist(store, task):
    with store.sessions.begin() as session:
        session.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.id == task.id).values(
            status=task.status, task_snapshot=task.model_dump(mode="json")))


async def test_complete_review_automatically_publishes_summary_and_inline(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    assert await service.worker.run_once()
    assert status(service, task).state == "queued"
    assert api.writes == []
    assert await service.worker.run_once()
    assert status(service, task).state == "succeeded"
    assert len(api.comments) == len(api.inline) == len(api.reviews) == 1
    assert api.reviews[100]["commit_id"] == task.snapshot.head_sha
    assert not await service.worker.run_once()
    assert store.get(task.id).status == "completed"


async def test_auto_job_waits_for_durable_terminal_report(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    task.status = "running"
    persist(store, task)
    assert not await service.worker.run_once()
    assert status(service, task).state == "waiting_review"
    task.status = "completed"
    persist(store, task)
    assert await service.worker.run_once()
    assert status(service, task).state == "queued"
    assert api.writes == []


@pytest.mark.parametrize("run_status,expected", [
    ("partial", "manual_required"), ("failed", "blocked"), ("cancelled", "blocked"),
])
async def test_partial_failed_and_cancelled_do_not_publish(publication_pipeline, run_status, expected):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    task.status = run_status
    persist(store, task)
    assert await service.worker.run_once()
    assert status(service, task).state == expected
    assert api.writes == []
    assert store.get(task.id).status == run_status
    assert not await service.worker.run_once()


async def test_clean_result_updates_summary_without_approving(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    task.report.candidates = []
    task.report.findings = []
    persist(store, task)
    assert await service.worker.run_once()
    assert await service.worker.run_once()
    assert status(service, task).state == "succeeded"
    assert len(api.comments) == 1
    assert api.reviews == {}


async def test_unresolved_result_requires_manual_confirmation(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    task.report.candidates[0].verification.verdict = "insufficient"
    task.report.candidates[0].finding = None
    task.report.findings = []
    persist(store, task)
    assert await service.worker.run_once()
    assert status(service, task).state == "manual_required"
    assert api.writes == []


async def test_unmappable_inline_requires_manual_confirmation(publication_pipeline):
    task, api, service, _, _ = await setup_automatic(publication_pipeline)
    original = service.preflight_checker.github.paginate

    async def incomplete(path):
        return [] if path.endswith("/files") else await original(path)

    service.preflight_checker.github.paginate = incomplete
    assert await service.worker.run_once()
    assert status(service, task).state == "manual_required"
    assert api.writes == []


@pytest.mark.parametrize("change", ["head", "permission", "expired", "publisher"])
async def test_changed_publication_conditions_never_write(publication_pipeline, change):
    expiry = {"expires_at": utcnow() - timedelta(seconds=1)} if change == "expired" else None
    task, api, service, _, _ = await setup_automatic(publication_pipeline, grant_update=expiry)
    if change == "head":
        api.head = "0" * 40
    if change == "permission":
        api.permission = False
    if change == "publisher":
        service.config.rivu_github_publish_publisher_id = 8
    assert await service.worker.run_once()
    assert status(service, task).state == "blocked"
    assert api.writes == []


async def test_crash_after_outbox_creation_recovers_same_attempt_even_if_report_changes(
        publication_pipeline):
    task, api, service, store, job_id = await setup_automatic(publication_pipeline)
    original = service.worker.queue.complete
    service.worker.queue.complete = lambda *args, **kwargs: False
    assert await service.worker.run_once()
    identity = status(service, task).publication_id
    with store.sessions.begin() as session:
        session.execute(update(WorkerJobOrm).where(WorkerJobOrm.id == job_id).values(
            lease_until=utcnow() - timedelta(seconds=1)))
    task.report.version = "f" * 64
    persist(store, task)
    service.worker.queue.complete = original
    assert await service.worker.run_once()
    assert status(service, task).publication_id == identity
    with store.sessions() as session:
        assert len(list(session.scalars(select(PublicationAttemptOrm)))) == 1
    assert api.writes == []
    assert await service.worker.run_once()
    assert status(service, task).state == "failed"
    assert store.get(task.id).status == "completed"
    assert api.writes == []


@pytest.mark.parametrize("initial_status,decision", [("partial", "manual_required"), ("failed", "blocked")])
async def test_saved_no_publish_decision_is_not_replayed_after_lost_completion(
        publication_pipeline, initial_status, decision):
    task, api, service, store, job_id = await setup_automatic(publication_pipeline)
    task.status = initial_status
    persist(store, task)
    original = service.worker.queue.complete
    service.worker.queue.complete = lambda *args, **kwargs: False
    assert await service.worker.run_once()
    assert status(service, task).state == decision
    with store.sessions.begin() as session:
        session.execute(update(WorkerJobOrm).where(WorkerJobOrm.id == job_id).values(
            lease_until=utcnow() - timedelta(seconds=1)))
    task.status = "completed"
    persist(store, task)
    service.worker.queue.complete = original
    assert await service.worker.run_once()
    assert status(service, task).state == decision
    assert service.repo.list(task.id) == []
    assert api.writes == []


async def test_concurrent_dispatch_creates_one_durable_attempt(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    job = DatabaseTaskQueue(store.sessions).claim(worker_id="dispatch", kinds=("automatic_publication",))
    await asyncio.gather(service.dispatch_automatic(job), service.dispatch_automatic(job))
    with store.sessions() as session:
        assert len(list(session.scalars(select(PublicationAttemptOrm)))) == 1
    assert status(service, task).state == "queued"
    assert api.writes == []


async def test_lost_dispatch_lease_cannot_create_outbox(publication_pipeline):
    task, api, service, store, job_id = await setup_automatic(publication_pipeline)
    job = DatabaseTaskQueue(store.sessions).claim(worker_id="dispatch", kinds=("automatic_publication",))
    with store.sessions.begin() as session:
        session.execute(update(WorkerJobOrm).where(WorkerJobOrm.id == job_id).values(
            lease_until=utcnow() - timedelta(seconds=1)))
    with pytest.raises(PublicationLeaseLost):
        await service.dispatch_automatic(job)
    assert service.repo.list(task.id) == []
    assert api.writes == []


@pytest.mark.parametrize("kind", ["summary", "inline"])
async def test_unknown_write_stays_unknown_until_explicit_reconciliation(publication_pipeline, kind):
    task, api, service, _, _ = await setup_automatic(publication_pipeline)
    assert await service.worker.run_once()
    setattr(api, "timeout_" + kind, True)
    assert await service.worker.run_once()
    assert status(service, task).state == "uncertain"
    assert not await service.worker.run_once()
    identity = status(service, task).publication_id
    service.repo.retry(identity)
    assert await service.worker.run_once()
    assert status(service, task).state == "succeeded"
    assert len(api.comments) == len(api.inline) == len(api.reviews) == 1


async def test_expiry_after_dispatch_prevents_new_github_writes(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    assert await service.worker.run_once()
    identity = status(service, task).publication_id
    with store.sessions.begin() as session:
        attempt = session.get(PublicationAttemptOrm, identity)
        grant = {**attempt.context["automatic_authorization"],
                 "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
        attempt.context = {**attempt.context, "automatic_authorization": grant}
    assert await service.worker.run_once()
    assert status(service, task).state == "failed"
    assert api.writes == []


@pytest.mark.parametrize("operation", ["retry", "delete", "retention"])
async def test_disabled_pending_automatic_job_does_not_block_explicit_lifecycle(
        publication_pipeline, operation):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    service.config.rivu_github_publish_enabled = False
    assert automatic_publication_status(store.sessions, task.id, enabled=False).state == "paused"
    if operation == "retry":
        task.status = task.report.status = "partial"
        persist(store, task)
        store.retry(task.id)
        assert store.get(task.id).status == "queued"
        assert status(service, task).state == "blocked"
    else:
        if operation == "retention":
            with store.sessions.begin() as session:
                session.get(ReviewTaskOrm, task.id).retention_until = utcnow() - timedelta(days=1)
        assert store.delete_task(task.id, require_retention=operation == "retention")
    assert api.writes == []


@pytest.mark.parametrize("operation", ["retry", "delete", "retention"])
async def test_disabled_unsent_auto_outbox_can_be_revoked_by_lifecycle(
        publication_pipeline, operation):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    assert await service.worker.run_once()
    identity = status(service, task).publication_id
    service.config.rivu_github_publish_enabled = False
    assert automatic_publication_status(store.sessions, task.id, enabled=False).state == "paused"
    if operation == "retry":
        task.status = task.report.status = "partial"
        persist(store, task)
        store.retry(task.id)
        assert store.get(task.id).status == "queued"
    else:
        if operation == "retention":
            with store.sessions.begin() as session:
                session.get(ReviewTaskOrm, task.id).retention_until = utcnow() - timedelta(days=1)
        assert store.delete_task(task.id, require_retention=operation == "retention")
    attempt = service.repo.get(identity)
    assert attempt.state == "failed"
    assert attempt.last_error_code == "automatic_publication_cancelled"
    assert api.writes == []


@pytest.mark.parametrize("delivery_state", ["preparing", "publishing", "uncertain"])
async def test_started_or_unknown_auto_outbox_is_never_discarded(
        publication_pipeline, delivery_state):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    assert await service.worker.run_once()
    identity = status(service, task).publication_id
    with store.sessions.begin() as session:
        session.get(PublicationAttemptOrm, identity).state = delivery_state
    service.config.rivu_github_publish_enabled = False
    with pytest.raises(ValueError, match="publication_pending"):
        store.delete_task(task.id)
    assert service.repo.get(identity).state == delivery_state
    assert api.writes == []


async def test_active_dispatch_or_delivery_lease_blocks_deletion(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    queue = DatabaseTaskQueue(store.sessions)
    job = queue.claim(worker_id="dispatch", kinds=("automatic_publication",))
    with pytest.raises(ValueError, match="review_job_active"):
        store.delete_task(task.id)
    await service.dispatch_automatic(job)
    assert queue.complete(job.id, job.lease_owner, attempt=job.attempts)
    delivery = queue.claim(worker_id="delivery", kinds=("github_publication",))
    assert delivery is not None
    with pytest.raises(ValueError, match="publication_pending"):
        store.delete_task(task.id)
    assert api.writes == []


async def test_claim_rechecks_terminal_status_in_atomic_update(publication_pipeline):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    engine = store.sessions.kw["bind"]
    changed = False

    def move_run_after_candidate(connection, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if not changed and statement.lstrip().startswith("SELECT worker_jobs.id"):
            changed = True
            connection.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.id == task.id).values(status="running"))

    event.listen(engine, "after_cursor_execute", move_run_after_candidate)
    try:
        assert DatabaseTaskQueue(store.sessions).claim(
            worker_id="dispatch", kinds=("automatic_publication",)) is None
        assert changed
    finally:
        event.remove(engine, "after_cursor_execute", move_run_after_candidate)
    assert api.writes == []


@pytest.mark.parametrize("stage", ["automatic", "outbox"])
async def test_lifecycle_write_lock_prevents_claim_between_retirement_checks_and_commit(
        publication_pipeline, monkeypatch, stage):
    task, api, service, store, _ = await setup_automatic(publication_pipeline)
    if stage == "outbox":
        assert await service.worker.run_once()
    engine = store.sessions.kw["bind"]
    entered, attempted_claim, release = Event(), Event(), Event()
    original = store._retire_unsent_automatic_publication
    claimant = None

    def paused_retirement(session, run_id):
        entered.set()
        assert release.wait(10)
        return original(session, run_id)

    def observe_claim(connection, cursor, statement, parameters, context, executemany):
        if get_ident() == claimant and statement.lstrip().startswith("UPDATE worker_jobs"):
            attempted_claim.set()

    def claim():
        nonlocal claimant
        claimant = get_ident()
        return DatabaseTaskQueue(store.sessions).claim(
            worker_id="racing-publisher",
            kinds=("automatic_publication",) if stage == "automatic" else ("github_publication",))

    monkeypatch.setattr(store, "_retire_unsent_automatic_publication", paused_retirement)
    event.listen(engine, "before_cursor_execute", observe_claim)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            deletion = pool.submit(store.delete_task, task.id)
            try:
                assert entered.wait(10)
                claiming = pool.submit(claim)
                assert attempted_claim.wait(10)
            finally:
                release.set()
            assert deletion.result(timeout=10)
            assert claiming.result(timeout=10) is None
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", observe_claim)
    assert api.writes == []
