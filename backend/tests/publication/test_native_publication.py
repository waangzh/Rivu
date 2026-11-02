import pytest

from app.services.publication.publication_preflight import PublicationPreflight
from app.tools.github.github_publisher import GitHubPublishError


class Config:
    rivu_github_publish_enabled = True
    rivu_github_publish_publisher_id = 7
    rivu_github_publish_allowed_repositories = "123"
    rivu_github_publish_pull_requests_write_confirmed = True
    rivu_github_publish_max_inline = 10


class GitHub:
    def __init__(self, run):
        self.head = run.snapshot.head_sha
        self.base = run.snapshot.declared_base_sha
        self.permission = True

    def repo_path(self, owner, repo):
        return f"/repos/{owner}/{repo}"

    async def get(self, path):
        if path == "/user":
            return {"id": 7}
        if path == "/repos/local/repo":
            return {"id": 123, "full_name": "local/repo", "permissions": {"push": self.permission}}
        if path == "/repos/local/repo/pulls/1":
            return {"number": 1, "state": "open", "merged": False, "changed_files": 1,
                "head": {"sha": self.head}, "base": {"sha": self.base, "repo": {"id": 123}}}
        raise AssertionError(path)

    async def paginate(self, path):
        assert path == "/repos/local/repo/pulls/1/files"
        return [{"filename": "safe.py", "status": "modified", "additions": 1, "deletions": 1,
            "patch": "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2"}]


async def test_preflight_keeps_identity_permission_and_precise_right_mapping(normalized_run):
    run, artifacts = normalized_run
    checked = await PublicationPreflight(GitHub(run), Config(), artifacts).check(run)
    assert checked["report_version"] == checked["plan"].report_version
    assert checked["locations"]["finding-candidate"] == {"path": "safe.py", "line": 1, "side": "RIGHT"}


async def test_preflight_rejects_stale_head_and_permission(normalized_run):
    run, artifacts = normalized_run
    github = GitHub(run)
    github.head = "0" * 40
    with pytest.raises(GitHubPublishError, match="publication_stale"):
        await PublicationPreflight(github, Config(), artifacts).check(run)
    github.head, github.permission = run.snapshot.head_sha, False
    with pytest.raises(GitHubPublishError, match="github_permission_denied"):
        await PublicationPreflight(github, Config(), artifacts).check(run)


async def test_unresolved_with_confirmed_findings_still_requires_partial_acknowledgement(normalized_run):
    from app.review.domain import VerificationQuestion
    from app.review.findings import finding_digest

    run, artifacts = normalized_run
    candidate = run.report.candidates[0].model_copy(deep=True)
    candidate.id = "pending"
    candidate.finding = None
    candidate.verification.verdict = "insufficient"
    candidate.verification.question = VerificationQuestion(
        observation="源码返回不同的值。", missing_premise="接口允许值未知。", question="请确认允许值。")
    candidate.question_digest = finding_digest(candidate, run.snapshot)
    run.report.candidates.append(candidate)
    checked = await PublicationPreflight(GitHub(run), Config(), artifacts).check(run)
    assert checked["plan"].outcome.kind == "findings"
    assert checked["plan"].outcome.coverage == "complete"
    assert checked["requires_partial_acknowledgement"]
    assert "### 待核实项" in checked["plan"].review_comment_markdown
