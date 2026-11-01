import sys
import threading

import pytest

from app.tools.git_tool import GitTool, GitToolCancelled, GitToolError, GitToolTimeout
from tests.review.test_production_diff_base import _git, _head_and_base_repo, _pr_with_heads


def test_deleted_branch_head_is_fetched_from_pull_ref_without_changing_snapshot(tmp_path):
    source, base, head = _head_and_base_repo(tmp_path)
    _git(source, "update-ref", "refs/pull/1/head", head)
    _git(source, "update-ref", "refs/heads/main", base)

    class ShaUnavailable(GitTool):
        def _run(self, command, **kwargs):
            if "fetch" in command and command[-1] == head:
                raise GitToolError("server rejects direct SHA fetch")
            return super()._run(command, **kwargs)

    pr = _pr_with_heads(source, base, head)
    pr.head.ref = "deleted"
    tool = ShaUnavailable(tmp_path / "workspaces")
    repo, diff = tool.clone_and_diff(pr)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert "+x = 2" in diff


def test_pull_ref_cannot_replace_missing_declared_head(tmp_path):
    source, base, head = _head_and_base_repo(tmp_path)
    _git(source, "update-ref", "refs/pull/1/head", head)
    pr = _pr_with_heads(source, base, head, head_sha="f" * 40)
    pr.head.ref = "deleted"
    tool = GitTool(tmp_path / "workspaces")
    with pytest.raises(GitToolError, match="缺少请求的提交"):
        tool.clone_and_diff(pr)
    assert not list(tool._workdir.iterdir())


def test_transport_error_retries_same_ref_and_preserves_safe_failure(tmp_path):
    tool = GitTool(tmp_path, command_timeout_seconds=5)
    attempts = []

    def fail(command, **kwargs):
        attempts.append(command[-1])
        return GitTool._run(tool, [sys.executable, "-c",
            "import sys; sys.stderr.write('fatal: Failed to connect to github.com port 443: "
            "Timed out PRIVATE_DETAIL'); sys.exit(128)"], **kwargs)

    tool._run = fail
    tool._commit_exists = lambda *args, **kwargs: False
    with pytest.raises(GitToolError) as caught:
        tool._fetch_ref(tmp_path, "origin", "a" * 40, "deleted", phase="fetch_head",
                        phase_label="head", progress_callback=None, cancel_event=None)
    assert attempts == ["a" * 40] * 3
    assert caught.value.public_detail()["code"] == "git_connection_failed"
    assert caught.value.public_detail()["retryable"] is True
    assert "PRIVATE_DETAIL" not in str(caught.value.public_detail())


@pytest.mark.parametrize("error", [GitToolCancelled("cancelled"), GitToolTimeout("timeout")])
def test_fallback_keeps_cancellation_and_timeout_types(tmp_path, error):
    tool = GitTool(tmp_path)
    attempts = []

    def fail(command, **kwargs):
        attempts.append(command[-1])
        if len(attempts) == 1:
            raise GitToolError("SHA unavailable")
        raise error

    tool._run = fail
    tool._commit_exists = lambda *args, **kwargs: False
    with pytest.raises(type(error)):
        tool._fetch_ref(tmp_path, "origin", "a" * 40, "deleted", phase="fetch_head",
                        phase_label="head", progress_callback=None, cancel_event=None)
    assert len(attempts) == 2


def test_cancelled_retry_backoff_stops_without_next_fetch(tmp_path):
    tool = GitTool(tmp_path)
    cancel = threading.Event()

    def fail(command, **kwargs):
        cancel.set()
        raise GitToolError("fatal: Failed to connect to github.com port 443: Timed out")

    tool._run = fail
    tool._commit_exists = lambda *args, **kwargs: False
    with pytest.raises(GitToolCancelled):
        tool._fetch_ref(tmp_path, "origin", "a" * 40, "deleted", phase="fetch_head",
                        phase_label="head", progress_callback=None, cancel_event=cancel)


def test_remote_retries_share_command_deadline(tmp_path):
    tool = GitTool(tmp_path, command_timeout_seconds=0.1)
    with pytest.raises(GitToolTimeout):
        tool._run_remote([sys.executable, "-c",
            "import sys; sys.stderr.write('fatal: Failed to connect: Timed out'); sys.exit(128)"])


def test_transient_fetch_retry_recovers_without_branch_fallback(tmp_path):
    tool = GitTool(tmp_path)
    attempts = []
    present = False

    def run(command, **kwargs):
        nonlocal present
        attempts.append(command[-1])
        if len(attempts) == 1:
            raise GitToolError("fatal: Failed to connect: Timed out")
        present = True
        return ""

    tool._run = run
    tool._commit_exists = lambda *args, **kwargs: present
    tool._fetch_ref(tmp_path, "origin", "a" * 40, "deleted", phase="fetch_head",
                    phase_label="head", progress_callback=None, cancel_event=None)
    assert attempts == ["a" * 40] * 2


def test_remote_retry_does_not_retry_permanent_error(tmp_path):
    tool = GitTool(tmp_path)
    attempts = []

    def fail(command, **kwargs):
        attempts.append(command)
        raise GitToolError("fatal: Authentication failed PRIVATE_CREDENTIAL")

    tool._run = fail
    with pytest.raises(GitToolError) as caught:
        tool._run_remote(["git", "fetch", "origin", "a" * 40])
    assert len(attempts) == 1
    assert caught.value.public_detail()["retryable"] is False
    assert "PRIVATE" not in str(caught.value.public_detail())
