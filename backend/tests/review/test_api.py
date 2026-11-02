import asyncio
import shutil
import logging
import json
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.review.contracts import ReviewRequest
from app.services.review.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool, GitToolError, GitToolTimeout
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_source_run


@pytest.fixture
def service_pipeline(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.api import reviews
    store, source_run, _, source_provider, _ = setup_source_run(tmp_path)
    source_run.pr.repository_id = 1
    workdir = tmp_path / "workspaces"
    workdir.mkdir()

    class FixedGit(GitTool):
        def clone_and_diff(self, pr, **kwargs):
            target = self._workdir / ("fixture-" + uuid4().hex)
            shutil.copytree(source_provider.evidence.repo_path, target)
            _, diff = self.unified_diff(target, pr.base.sha, pr.head.sha)
            return target, diff

    class GitHub:
        calls = 0
        pr = source_run.pr
        source_repo = source_provider.evidence.repo_path
        async def fetch_pr(self, url):
            self.calls += 1
            return self.pr.model_copy(deep=True)

    github = GitHub()
    model = ReviewModel()
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost",
        _default_model="fake", agent_model=lambda *args, **kwargs: model)
    monkeypatch.setattr(settings, "rivu_pr_discussions_enabled", False)
    monkeypatch.setattr(settings, "rivu_prepared_workspace_max_entries", 0)
    service = ReviewService(github, FixedGit(workdir), DiffParser(), provider, store,
                            source_provider.evidence.artifacts)
    monkeypatch.setattr(service, "start_worker", lambda: None)
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    return service, github, app


async def test_direct_create_is_idempotent_and_complete_report_uses_new_api(service_pipeline):
    service, github, app = service_pipeline
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"pr_url": "https://github.com/o/r/pull/1"}
        first = await client.post("/api/reviews", json=body, headers={"Idempotency-Key": "create"})
        assert first.status_code == 202, first.text
        run_id = first.json()["id"]
        second = await client.post("/api/reviews", json=body, headers={"Idempotency-Key": "create"})
        assert second.json()["id"] == run_id
        assert github.calls == 1
        changed = await client.post("/api/reviews", json={**body, "depth": "deep"},
                                    headers={"Idempotency-Key": "create"})
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "idempotency_key_conflict"
        assert (await client.post("/api/reviews", json=body)).status_code == 422
        assert await service._worker.run_once()
        result = (await client.get(f"/api/reviews/{run_id}")).json()
        assert result["status"] == "completed", result
        assert result["report"]["overview"]["status"] == "ready"
        assert len(result["report"]["findings"]) == 1
        assert "goals" not in result["report"]
        report = (await client.get(f"/api/reviews/{run_id}/report")).json()
        assert "更新比例计算逻辑" in report["markdown"]
        plan = (await client.get(f"/api/reviews/{run_id}/publication-plan")).json()
        assert plan["report_version"] == report["report"]["version"]
        events = await client.get(f"/api/reviews/{run_id}/events")
        assert "event: done" in events.text
        assert len((await client.get(f"/api/reviews/{run_id}/units")).json()) == 2
        assert (await client.post(f"/api/reviews/{run_id}/resume")).status_code == 404
        assert result["report"]["findings"][0]["publishable"] is True


async def test_overview_retry_only_calls_overview_and_changes_report_version(service_pipeline):
    service, _, _ = service_pipeline
    run = await service.create(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), "first")
    assert await service._worker.run_once()
    before = service.store.get(run.id)
    assert before.report.usage["model_calls"] == 4
    await service.retry(run.id, overview=True)
    assert await service._worker.run_once()
    after = service.store.get(run.id)
    assert after.status == "completed"
    assert after.report.usage["model_calls"] == 5
    assert after.report.version != before.report.version
    assert after.report.findings == before.report.findings
    assert after.hard_deadline == before.hard_deadline


async def test_optional_preview_does_not_create_a_run_or_call_model(service_pipeline):
    service, _, _ = service_pipeline
    before = service.store.list_runs()["total"]
    preview = await service.preview(ReviewRequest(pr_url="https://github.com/o/r/pull/1"))
    assert preview["plan"].units
    assert service.store.list_runs()["total"] == before


async def test_large_pr_groups_only_after_create_and_keeps_every_file(service_pipeline):
    service, github, _ = service_pipeline
    for name in ("b.py", "c.py", "d.py"):
        (github.source_repo / name).write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(github.source_repo), "add", "."],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(github.source_repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.test", "commit", "-m", "group fixture"],
                   check=True, capture_output=True)
    github.pr.head.sha = subprocess.run(
        ["git", "-C", str(github.source_repo), "rev-parse", "HEAD"],
        check=True, capture_output=True).stdout.decode().strip()
    calls = []

    class GroupedReviewModel(ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if str(messages[0].content).startswith("Group changed files"):
                packet = json.loads(messages[1].content)
                calls.append(packet)
                assert len(packet["files"]) == 4
                assert "VALUE = 1" not in messages[1].content
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="", usage_metadata={"input_tokens": 80, "output_tokens": 20,
                                                 "total_tokens": 100}, tool_calls=[{
                        "name": "GroupingOutput", "args": {"groups": [
                            {"label": "Feature pair", "files": [0, 3]},
                            {"label": "Support pair", "files": [1, 2]},
                        ]}, "id": "grouping",
                    }]))])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = GroupedReviewModel(empty=True)
    service._provider.agent_model = lambda *args, **kwargs: model
    request = ReviewRequest(pr_url=github.pr.html_url)
    preview = await service.preview(request)
    assert preview["plan"].grouping_status == "deterministic"
    assert calls == []
    run = await service.create(request, "grouped-create")
    assert run.plan.grouping_status == "pending"
    assert service.store.work_items(run.id) == []
    assert calls == []
    assert await service._worker.run_once()
    result = service.store.get(run.id)
    assert result.status == "completed", result.report.reasons
    assert result.plan.grouping_status == "model"
    assert sorted(path for unit in result.plan.units for path in unit.paths) == [
        "a.py", "b.py", "c.py", "d.py"]
    assert len(service.store.work_items(run.id)) == 2
    assert result.report.usage["model_calls"] == 4
    assert len(calls) == 1


async def test_cancel_preserves_candidate_facts_and_rejects_retry(service_pipeline):
    service, _, app = service_pipeline
    run = await service.create(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), "cancel")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/reviews/{run.id}/cancel")).status_code == 202
        current = (await client.get(f"/api/reviews/{run.id}")).json()
        assert current["status"] == "cancelled"
        assert current["report"]["overview"]["status"] == "skipped_cancelled"
        assert (await client.post(f"/api/reviews/{run.id}/retry")).status_code == 409


async def test_delete_removes_source_artifacts_but_keeps_creation_tombstone(service_pipeline):
    service, github, _ = service_pipeline
    request = ReviewRequest(pr_url=github.pr.html_url)
    run = await service.create(request, "delete")
    assert await service._worker.run_once()
    directory = service._artifacts.root / run.id
    assert directory.is_dir()
    assert service.delete_task(run.id)
    assert not directory.exists()
    assert service.get_task(run.id) is None
    assert service.store.candidates(run.id) == []
    assert service.store.work_items(run.id) == []
    with pytest.raises(ValueError, match="review_deleted"):
        await service.create(request, "delete")


async def test_review_logs_correlate_lifecycle_without_review_content(service_pipeline, caplog):
    service, github, app = service_pipeline
    github.pr.title = "PRIVATE_TITLE_MUST_NOT_BE_LOGGED"
    github.pr.body = "PRIVATE_DESCRIPTION_MUST_NOT_BE_LOGGED"
    caplog.set_level(logging.INFO, logger="Rivu.Review")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/reviews", json={"pr_url": github.pr.html_url},
                                     headers={"Idempotency-Key": "log-lifecycle"})
        assert response.status_code == 202
        run_id = response.json()["id"]
        assert await service._worker.run_once()
    records = [record for record in caplog.records if record.name.startswith("Rivu.Review")]
    messages = [record.getMessage() for record in records]
    text = "\n".join(messages)
    for event in ("审查准备开始", "审查规划完成", "审查已入队", "作业已领取", "Agent开始",
                  "模型请求开始", "模型请求返回", "候选已保存并排队核验", "总览生成完成",
                  "审查结束", "作业完成回执已保存"):
        assert any(event in message and f"review_id={run_id[:8]}" in message for message in messages)
    assert run_id not in text
    assert "verdict=confirmed" in text
    assert "status=completed" in text
    assert "tools=submit_candidate" in text
    for private in (github.pr.title, github.pr.body, "return 10 / x", "零输入导致除零",
                    "source_refs", "http://localhost", "Idempotency-Key"):
        assert private not in text
    assert all(record.exc_info is None for record in records)


async def test_job_failure_logs_error_type_without_exception_body(service_pipeline, caplog):
    service, github, _ = service_pipeline
    caplog.set_level(logging.INFO, logger="Rivu.Review")
    run = await service.create(ReviewRequest(pr_url=github.pr.html_url), "log-job-failure")

    def unavailable(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_CREDENTIAL_AND_RESPONSE")

    service._provider.agent_model = unavailable
    assert await service._worker.run_once()
    text = "\n".join(record.getMessage() for record in caplog.records
                     if record.name.startswith("Rivu.Review"))
    assert f"审查作业失败 review_id={run.id[:8]}" in text
    assert "status=dead_letter" in text
    assert "error_type=RuntimeError" in text
    assert "PRIVATE_PROVIDER_CREDENTIAL_AND_RESPONSE" not in text
    assert service.store.get(run.id).status == "failed"


@pytest.mark.parametrize("path", ["/api/reviews", "/api/reviews/preview"])
@pytest.mark.parametrize("error,code,status", [
    (GitToolError("PRIVATE_GIT_URL_AND_STDERR"), "git_preparation_failed", 502),
    (GitToolTimeout("PRIVATE_GIT_URL_AND_STDERR"), "git_timeout", 504),
])
async def test_preparation_failure_has_safe_error_protocol(service_pipeline, monkeypatch,
                                                          path, error, code, status):
    service, github, app = service_pipeline

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(service, "_prepare", fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                base_url="http://test") as client:
        response = await client.post(path, json={"pr_url": github.pr.html_url},
                                     headers={"Idempotency-Key": "preparation-failure"})
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["retryable"] is (status == 504)
    assert "PRIVATE" not in response.text
    assert not service._creations


async def test_concurrent_creation_shares_preparation_and_survives_waiter_cancel(
        service_pipeline, monkeypatch):
    service, github, _ = service_pipeline
    entered, release = asyncio.Event(), asyncio.Event()
    original, preparations = service._inputs, []

    async def blocked(*args, **kwargs):
        preparations.append(True)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_inputs", blocked)
    request = ReviewRequest(pr_url=github.pr.html_url)
    first = asyncio.create_task(service.create(request, "shared"))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(service.create(request, "shared"))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="idempotency_key_conflict"):
        await service.create(request.model_copy(update={"depth": "deep"}), "shared")
    release.set()
    result = await second
    assert preparations == [True]
    assert github.calls == 1
    assert (await service.create(request, "shared")).id == result.id
    assert not service._creations


async def test_failed_preparation_releases_same_key_for_retry(service_pipeline, monkeypatch):
    service, github, _ = service_pipeline
    original = service._inputs

    async def fail(*args, **kwargs):
        raise GitToolError("unavailable")

    monkeypatch.setattr(service, "_inputs", fail)
    request = ReviewRequest(pr_url=github.pr.html_url)
    with pytest.raises(GitToolError):
        await service.create(request, "retry-preparation")
    assert not service._creations
    monkeypatch.setattr(service, "_inputs", original)
    assert (await service.create(request, "retry-preparation")).status == "queued"


async def test_close_cancels_shared_preparation(service_pipeline, monkeypatch):
    service, github, _ = service_pipeline
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(service, "_inputs", blocked)
    pending = asyncio.create_task(service.create(ReviewRequest(pr_url=github.pr.html_url), "closing"))
    await entered.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stopped.is_set()
    assert not service._creations
