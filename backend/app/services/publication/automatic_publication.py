"""Per-review operator grants, persisted in the existing durable job queue."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

from app.models.orm import PublicationAttemptOrm, WorkerJobOrm
from app.models.publication import AutomaticPublicationAuthorization, AutomaticPublicationStatus
from app.review.domain import TERMINAL, utcnow
from app.review.store import as_utc
from app.services.publication.publication_repository import PublicationLeaseLost
from app.tools.github.github_publisher import GitHubPublishError


@dataclass(frozen=True)
class AutomaticPublicationPolicy:
    publisher_id: int
    repositories: frozenset[int]
    max_inline: int

    @classmethod
    def from_config(cls, config):
        if not config.rivu_github_publish_enabled:
            raise GitHubPublishError("publication_disabled")
        if (not config.rivu_github_publish_token
                or not config.rivu_github_publish_publisher_id
                or not config.rivu_github_publish_pull_requests_write_confirmed):
            raise GitHubPublishError("github_permission_denied")
        return cls(config.rivu_github_publish_publisher_id, frozenset(
            int(value.strip()) for value in config.rivu_github_publish_allowed_repositories.split(",")
            if value.strip().isdigit()), config.rivu_github_publish_max_inline)

    def authorize(self, run):
        if run.snapshot.repository_id not in self.repositories:
            raise GitHubPublishError("repository_not_authorized")
        return AutomaticPublicationAuthorization(
            review_id=run.id, repository_id=run.snapshot.repository_id,
            pr_number=run.pr.number, base_sha=run.snapshot.declared_base_sha,
            head_sha=run.snapshot.head_sha, publisher_identity=f"user:{self.publisher_id}",
            max_inline=self.max_inline, authorized_at=utcnow(),
            expires_at=run.hard_deadline + timedelta(hours=24))


def validate_automatic_authorization(raw, context, head_sha):
    grant = AutomaticPublicationAuthorization.model_validate(raw)
    if (grant.authorized_at.tzinfo is None or grant.expires_at.tzinfo is None
            or grant.expires_at <= utcnow()):
        raise GitHubPublishError("automatic_authorization_expired")
    if (grant.repository_id != context["repository_id"]
            or grant.pr_number != context["pr_number"]
            or grant.publisher_identity != context["publisher_identity"]
            or grant.base_sha != context["base_sha"] or grant.head_sha != head_sha):
        raise GitHubPublishError("automatic_authorization_mismatch")
    return grant


def automatic_job(session, review_id):
    return session.scalar(select(WorkerJobOrm).where(
        WorkerJobOrm.idempotency_key == f"automatic_publication:{review_id}",
        WorkerJobOrm.task_id == review_id, WorkerJobOrm.kind == "automatic_publication"))


def guard_automatic_job(session, job):
    updated = session.execute(update(WorkerJobOrm).where(
        WorkerJobOrm.id == job.id, WorkerJobOrm.task_id == job.task_id,
        WorkerJobOrm.kind == "automatic_publication", WorkerJobOrm.status == "leased",
        WorkerJobOrm.lease_owner == job.lease_owner, WorkerJobOrm.attempts == job.attempts,
        WorkerJobOrm.lease_until > utcnow()).values(updated_at=utcnow()))
    if updated.rowcount != 1:
        raise PublicationLeaseLost("publication_lease_lost")
    return session.get(WorkerJobOrm, job.id)


def automatic_publication_status(sessions, review_id, *, enabled):
    with sessions() as session:
        job = automatic_job(session, review_id)
        if job is None:
            return None
        payload = job.payload
        identity = payload.get("publication_id")
        if identity:
            attempt = session.get(PublicationAttemptOrm, identity)
            if attempt is not None:
                if not enabled and attempt.state == "queued":
                    return AutomaticPublicationStatus(state="paused", publication_id=identity,
                                                      last_error_code="publication_disabled")
                return AutomaticPublicationStatus(state=attempt.state, publication_id=identity,
                                                  last_error_code=attempt.last_error_code)
        state = payload.get("state", "waiting_review")
        code = payload.get("last_error_code")
        if state == "waiting_review":
            exhausted = job.status == "dead_letter" or (
                job.status == "leased" and job.attempts >= job.max_attempts
                and job.lease_until is not None and as_utc(job.lease_until) <= utcnow())
            if exhausted:
                state, code = "blocked", "automatic_publication_check_failed"
            elif not enabled:
                state, code = "paused", "publication_disabled"
        return AutomaticPublicationStatus(state=state, last_error_code=code)


def read_automatic_authorization(sessions, job):
    with sessions() as session:
        row = session.get(WorkerJobOrm, job.id)
        if (row is None or row.payload.get("publication_id")
                or row.payload.get("state") != "waiting_review"):
            return None
        return AutomaticPublicationAuthorization.model_validate(row.payload["authorization"])


def finish_automatic_check(sessions, job, state, code):
    with sessions.begin() as session:
        row = guard_automatic_job(session, job)
        row.payload = {**row.payload, "state": state, "last_error_code": code}


async def dispatch_automatic_publication(service, job):
    import asyncio

    grant = await asyncio.to_thread(read_automatic_authorization, service.repo.sessions, job)
    if grant is None:
        return
    task = await asyncio.to_thread(service.task_loader, job.task_id)
    state, code = None, None
    if task is None or task.status not in TERMINAL:
        raise GitHubPublishError("review_not_publishable")
    if task.status != "completed":
        state, code = ("manual_required" if task.status == "partial" else "blocked"), "review_" + task.status
    elif grant.review_id != task.id:
        state, code = "blocked", "automatic_authorization_mismatch"
    else:
        try:
            checked = await service.checked(task.id)
            validate_automatic_authorization(grant.model_dump(), checked["context"], checked["head_sha"])
            if checked["requires_partial_acknowledgement"]:
                state, code = "manual_required", "partial_acknowledgement_required"
            elif len(checked["plan"].inline_drafts) > grant.max_inline:
                state, code = "manual_required", "too_many_inline_findings"
            else:
                from app.models.publication import PublicationSelection

                checked["context"]["automatic_authorization"] = grant.model_dump(mode="json")
                selection = PublicationSelection(
                    expected_plan_hash=checked["plan_hash"],
                    expected_report_version=checked["report_version"],
                    expected_head_sha=checked["head_sha"], finding_keys=checked["finding_keys"])
                await asyncio.to_thread(service.repo.create, checked["plan"], selection,
                    checked["context"], f"automatic_publication:{task.id}", automatic_job=job)
                return
        except GitHubPublishError as exc:
            if exc.retry_after:
                from app.services.review.task_queue import RetryAfterJobError
                raise RetryAfterJobError(exc.code, exc.retry_after) from None
            if exc.code in {"github_transport_error", "github_request_failed", "github_response_invalid",
                            "github_pagination_incomplete"}:
                raise
            state = "manual_required" if exc.code in {"too_many_inline_findings", "partial_acknowledgement_required"} else "blocked"
            code = exc.code
    await asyncio.to_thread(finish_automatic_check, service.repo.sessions, job, state, code)
