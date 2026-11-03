import asyncio
import subprocess
import threading
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.orm import LocalReviewInputOrm, ReviewTaskOrm, WorkerJobOrm
from app.review.contracts import ReviewRequest
from app.review.domain import utcnow
from app.services.publication.publication_preflight import PublicationPreflight
from app.services.review.review_service import ReviewService
from app.services.workspaces.local_inputs import LocalInputError
from app.tools.github.github_publisher import GitHubPublishError
from app.tools.workspace_cleanup import cleanup_workspace

pytest_plugins = ["tests.review.test_api"]


def local_request(github, **options):
    return ReviewRequest(source={"kind": "local_git", "repository_key": "fixture",
        "base_ref": github.pr.base.sha, "head_ref": github.pr.head.sha}, **options)


@pytest.fixture
def local_pipeline(service_pipeline, monkeypatch):
    service, github, app = service_pipeline
    monkeypatch.setattr(settings, "rivu_local_repositories", {"fixture": github.source_repo})
    monkeypatch.setattr(settings, "rivu_pr_discussions_enabled", True)

    async def forbidden(*args, **kwargs):
        raise AssertionError("local reviews must not contact GitHub")

    github.fetch_pr = forbidden
    return service, github, app


async def test_local_review_survives_deleted_source_and_restarted_service(local_pipeline):
    service, github, app = local_pipeline
    request = local_request(github)
    preview = await service.preview(request)
    assert preview["source"].kind == "local_git"
    assert preview["snapshot"].pr_number is None
    assert preview["snapshot"].base_sha == github.pr.base.sha
    request.source.input_id = preview["source"].input_id
    assert cleanup_workspace(github.source_repo, workdir=github.source_repo.parent)
    run = await service.create(request, "local-review")
    assert run.pr is None and run.pr_url is None
    assert run.configuration.options.include_pr_discussions is False
    restarted = ReviewService(github, service._git_tool, service._diff_parser,
        service._provider, service.store, service._artifacts)
    assert await restarted._worker.run_once()
    result = restarted.get_task(run.id)
    assert result.status == "completed"
    assert len(result.report.findings) == 1
    assert service.local_inputs.context(result, 0).head_lines == ["def ratio(x):", "    return 10 / x"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        report = await client.get(f"/api/reviews/{run.id}/report")
        assert report.status_code == 200
        assert result.report.findings[0].claim in report.json()["markdown"]
        context = await client.get(f"/api/reviews/{run.id}/files/0/context")
        assert context.status_code == 200
        assert context.json()["head_lines"] == ["def ratio(x):", "    return 10 / x"]
        assert (await client.get("/api/reviews?q=fixture")).json()["total"] == 1
    assert (await service.create(request, "local-review")).id == run.id
    await restarted.close()


async def test_local_inputs_remain_pinned_until_review_deleted(local_pipeline):
    service, github, _ = local_pipeline
    run = await service.create(local_request(github), "pinned")
    with service.store.sessions.begin() as session:
        row = session.get(LocalReviewInputOrm, run.source.input_id)
        row.expires_at = utcnow() - timedelta(seconds=1)
    service.local_inputs.maintain()
    assert service.local_inputs.load(run.source.input_id)
    await service.cancel_task(run.id)
    service.delete_task(run.id)
    service.local_inputs.maintain()
    with pytest.raises(LocalInputError, match="local_input_expired"):
        service.local_inputs.load(run.source.input_id)


async def test_local_previews_expire_without_pinned_task(local_pipeline):
    service, github, _ = local_pipeline
    preview = await service.preview(local_request(github))
    with service.store.sessions.begin() as session:
        session.get(LocalReviewInputOrm, preview["source"].input_id).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(LocalInputError, match="local_input_expired"):
        service.local_inputs.load(preview["source"].input_id)
    service.local_inputs.maintain()
    assert not service.local_inputs._path(preview["source"].input_id).exists()


async def test_local_rejects_unregistered_repository_and_options(local_pipeline):
    service, github, _ = local_pipeline
    request = local_request(github)
    request.source.repository_key = "unknown"
    with pytest.raises(LocalInputError, match="not_registered"):
        await service.preview(request)
    for option in ({"auto_publish": True}, {"include_pr_discussions": True}):
        with pytest.raises(ValueError, match="local_source_has_no_github_capabilities"):
            local_request(github, **option)
    with pytest.raises(ValueError):
        ReviewRequest(pr_url=github.pr.html_url, source=local_request(github).source)


async def test_local_ignores_dirty_worktree_and_hostile_repository_config(local_pipeline, tmp_path):
    service, github, _ = local_pipeline
    marker = tmp_path / "executed"
    repo = github.source_repo
    (repo / "a.py").write_text("DIRTY WORKTREE MUST NOT BE REVIEWED")
    (repo / ".git/config").write_text(
        f'[core]\n\thooksPath = {marker.as_posix()}\n'
        f'[diff "hostile"]\n\ttextconv = touch {marker.as_posix()}\n'
        f'[filter "hostile"]\n\tsmudge = touch {marker.as_posix()}\n', encoding="utf-8")
    preview = await service.preview(local_request(github))
    repo_path = service.local_inputs._path(preview["source"].input_id) / "repo"
    assert (repo_path / "a.py").read_text() == "def ratio(x):\n    return 10 / x\n"
    assert not marker.exists()
    assert "hostile" not in (repo_path / ".git/config").read_text()


async def test_local_rejects_ref_options(local_pipeline):
    service, github, _ = local_pipeline
    request = local_request(github)
    request.source.head_ref = "--help"
    with pytest.raises(LocalInputError, match="local_ref_invalid"):
        await service.preview(request)


@pytest.mark.parametrize("kind", ["shallow", "partial", "alternates"])
async def test_local_import_freezes_borrowed_objects(local_pipeline, tmp_path, monkeypatch, kind):
    service, github, _ = local_pipeline
    original = github.source_repo
    imported = tmp_path / "imported"
    subprocess.run(["git", "-C", str(original), "config", "uploadpack.allowFilter", "true"],
                   check=True, capture_output=True)
    options = {"shallow": ["--depth=1"], "partial": ["--filter=blob:none", "--no-checkout"],
               "alternates": ["--shared"]}[kind]
    subprocess.run(["git", "clone", *options,
                    str(original) if kind == "alternates" else original.as_uri(), str(imported)],
                   check=True, capture_output=True)
    if kind == "shallow":
        subprocess.run(["git", "-C", str(imported), "fetch", "--depth=1", "origin", github.pr.base.sha],
                       check=True, capture_output=True)
    monkeypatch.setattr(settings, "rivu_local_repositories", {"fixture": imported})
    preview = await service.preview(local_request(github))
    frozen = service.local_inputs._path(preview["source"].input_id) / "repo"
    assert preview["snapshot"].base_sha == github.pr.base.sha
    assert not (frozen / ".git/objects/info/alternates").exists()
    assert not list((frozen / ".git/objects/pack").glob("*.promisor"))
    assert cleanup_workspace(imported, workdir=tmp_path)
    assert cleanup_workspace(original, workdir=original.parent)
    request = local_request(github)
    request.source.input_id = preview["source"].input_id
    run = await service.create(request, "borrowed-" + kind)
    assert await service._worker.run_once()
    assert service.get_task(run.id).status == "completed"


async def test_local_rejects_size_limit_and_cancellation(local_pipeline, monkeypatch):
    service, github, _ = local_pipeline
    monkeypatch.setattr(settings, "rivu_local_input_max_bytes", 1)
    with pytest.raises(LocalInputError, match="size_limit"):
        await service.preview(local_request(github))
    task = asyncio.create_task(service.preview(local_request(github)))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_local_reviews_do_not_supersede_one_another(local_pipeline):
    service, github, _ = local_pipeline
    first = await service.create(local_request(github), "first-local")
    repo = github.source_repo
    (repo / "a.py").write_text("def ratio(x):\n    return 20 / x\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
        "user.email=test@example.test", "commit", "-m", "next"], check=True, capture_output=True)
    request = local_request(github)
    request.source.head_ref = "HEAD"
    second = await service.create(request, "second-local")
    assert service.get_task(first.id).status == service.get_task(second.id).status == "queued"
    assert second.plan.incremental_from is None
    assert second.snapshot.head_sha != first.snapshot.head_sha
    with service.store.sessions() as session:
        assert len(list(session.scalars(select(ReviewTaskOrm).where(
            ReviewTaskOrm.input_id.is_not(None))))) == 2


async def test_local_publication_rejected_before_any_remote_access(local_pipeline):
    service, github, _ = local_pipeline
    run = await service.create(local_request(github), "unpublishable")
    with pytest.raises(GitHubPublishError, match="review_source_not_publishable"):
        await PublicationPreflight(None, None).check(run)


async def test_local_preparation_cancels_running_import_without_admitting_task(local_pipeline, monkeypatch):
    service, github, _ = local_pipeline
    entered, finished = threading.Event(), threading.Event()
    original = service.local_inputs._copy_objects
    original_freeze = service.local_inputs.freeze

    def copy_objects(source, destination, cancel):
        entered.set()
        while not cancel.is_set():
            finished.wait(0.01)
        return original(source, destination, cancel)

    def freeze(request, cancel):
        try:
            return original_freeze(request, cancel)
        finally:
            finished.set()

    monkeypatch.setattr(service.local_inputs, "_copy_objects", copy_objects)
    monkeypatch.setattr(service.local_inputs, "freeze", freeze)
    task = asyncio.create_task(service.preview(local_request(github)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 5)
    assert not [path for path in service.local_inputs.root.iterdir() if path.is_dir()]


async def test_local_frozen_input_rejects_changed_requested_refs_and_manifest(local_pipeline):
    service, github, _ = local_pipeline
    request = local_request(github)
    request.source.title = "PR title"
    request.source.body = "Author statement"
    preview = await service.preview(request)
    request.source.input_id = preview["source"].input_id
    request.source.head_ref = "another-branch"
    with pytest.raises(LocalInputError, match="identity_mismatch"):
        await service.preview(request)
    request.source.head_ref = github.pr.head.sha
    request.source.body = "Changed author statement"
    with pytest.raises(LocalInputError, match="identity_mismatch"):
        await service.preview(request)
    request.source.body = "Author statement"
    with service.store.sessions.begin() as session:
        row = session.get(LocalReviewInputOrm, request.source.input_id)
        row.manifest = {**row.manifest, "head_sha": "0" * 40}
    with pytest.raises(LocalInputError, match="identity_mismatch"):
        await service.preview(request)


async def test_local_import_rejects_object_directory_links(local_pipeline, tmp_path):
    service, github, _ = local_pipeline
    link = github.source_repo / ".git/objects/external"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this system")
    with pytest.raises(LocalInputError, match="local_repository_link"):
        await service.preview(local_request(github))


def test_local_registration_requires_safe_keys_and_absolute_paths(tmp_path):
    configured = Settings(_env_file=None, rivu_local_repositories={"project": tmp_path})
    assert configured.rivu_local_repositories["project"] == tmp_path
    for repositories in ({"../project": tmp_path}, {"project": "relative/repo"}):
        with pytest.raises(ValueError, match="local repositories require"):
            Settings(_env_file=None, rivu_local_repositories=repositories)


async def test_local_admission_rejects_input_expired_after_preparation(local_pipeline):
    service, github, _ = local_pipeline
    run = await service._inputs(local_request(github))
    with service.store.sessions.begin() as session:
        session.get(LocalReviewInputOrm, run.source.input_id).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(ValueError, match="local_input_expired"):
        service.store.create(run, "installation", "expired-admission", "request-hash")
    assert service.get_task(run.id) is None


async def test_local_overview_retry_retains_snapshot_work_and_usage(local_pipeline):
    service, github, _ = local_pipeline
    request = local_request(github)
    run = await service.create(request, "retry-local")
    assert await service._worker.run_once()
    completed = service.get_task(run.id)
    old_work = service.store.work_items(run.id)
    old_usage = service.store.usage(run.id)
    assert cleanup_workspace(github.source_repo, workdir=github.source_repo.parent)
    service.start_worker = lambda: None
    queued = await service.retry(run.id, overview=True)
    assert queued.snapshot == completed.snapshot
    assert queued.source == completed.source
    assert service.store.work_items(run.id) == old_work
    assert service.store.usage(run.id) == old_usage
    assert await service._worker.run_once()
    assert service.get_task(run.id).report.overview.status == "ready"
    assert service.store.usage(run.id)["model_calls"] > old_usage["model_calls"]


async def test_local_expired_lease_cannot_start_model_work(local_pipeline):
    service, github, _ = local_pipeline
    run = await service.create(local_request(github), "lease-local")
    job = service._task_queue.claim(worker_id="expired", lease_seconds=30, kinds=("review",))
    with service.store.sessions.begin() as session:
        session.get(WorkerJobOrm, job.id).lease_until = utcnow() - timedelta(seconds=1)
    await service._handle_job(job)
    assert service.store.usage(run.id)["model_calls"] == 0
    assert service.get_task(run.id).status == "queued"
