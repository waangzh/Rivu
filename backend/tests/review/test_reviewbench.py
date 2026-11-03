from types import SimpleNamespace

import pytest

from app.evaluation.reviewbench import export_findings, judge, main, review_sample, write_json
from app.services.review.review_service import ReviewService

pytest_plugins = ["tests.review.test_api"]


@pytest.mark.parametrize("skip_overview", [False, True])
async def test_reviewbench_runs_existing_review_and_exports_findings(
        service_pipeline, monkeypatch, skip_overview):
    service, github, _ = service_pipeline
    monkeypatch.setattr(service, "start_worker", lambda: ReviewService.start_worker(service))
    entry = {
        "nwo": "o/r", "repo": "https://github.com/o/r", "pr_number": 1,
        "base": github.pr.base.sha, "head": github.pr.head.sha,
        "title": "Frozen PR title", "body": "Frozen author statement",
    }
    try:
        output, detail = await review_sample(service, entry, github.source_repo, skip_overview=skip_overview)
        assert detail["status"] == "completed"
        assert detail["unresolved"] == 0
        assert output["pr"] == {key: entry[key] for key in ("repo", "pr_number", "base", "head")}
        assert len(output["findings"]) == 1
        assert output["findings"][0]["file"] == "a.py"
        assert output["findings"][0]["start_line"] == output["findings"][0]["end_line"]
        run = service.get_task(detail["review_id"])
        assert run.source.title == entry["title"]
        assert run.source.body == entry["body"]
        assert run.configuration.options.include_pr_discussions is False
        assert run.configuration.options.generate_overview is (not skip_overview)
        assert detail["overview"]["status"] == ("skipped_disabled" if skip_overview else "ready")
        assert detail["usage"]["model_calls"] == (3 if skip_overview else 4)
        if skip_overview:
            assert detail["overview"]["diagnostics"] == []
            with pytest.raises(ValueError, match="overview_disabled"):
                await service.retry(run.id, overview=True)
        run.report.findings[0].location.side = "base"
        run.report.usage["input_tokens"] = None
        projected, skipped = export_findings(run, entry, 100)
        assert projected["findings"] == []
        assert skipped[0]["reason"] == "no_head_location"
        assert "prompt_tokens" not in projected["usage"]
        assert "total_tokens" not in projected["usage"]
    finally:
        await service.close()


@pytest.mark.parametrize("findings", [[], [{"message": "A confirmed finding"}]])
def test_judge_accepts_zero_findings_without_rerunning_review(tmp_path, monkeypatch, findings):
    write_json(tmp_path / "findings/pr.json", {"findings": findings})
    arguments = []

    def execute(command, **kwargs):
        arguments.extend(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("app.evaluation.reviewbench.subprocess.run", execute)
    options = SimpleNamespace(benchmark_dir=tmp_path, judge_provider="deepseek",
                              judge_model="test", judge_concurrency=1)
    assert judge(options, tmp_path) == 0
    assert ("--allow-empty" in arguments) == (not findings)


def test_judge_only_cannot_retroactively_skip_overview(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "reviewbench", "--benchmark-dir", str(tmp_path), "--run-dir", str(tmp_path),
        "--judge-only", "--skip-overview", "--judge-provider", "fixture", "--judge-model", "fixture",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "--skip-overview" in capsys.readouterr().err
