import pytest

from app.review.findings import finding_digest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.publication_renderer import (
    overview_markdown, render_summary, render_user_report,
)


def unresolved_candidate(run):
    from app.review.domain import VerificationQuestion

    candidate = run.report.candidates[0].model_copy(deep=True)
    candidate.id = "question"
    candidate.finding = None
    candidate.proposal.claim = "UNVERIFIED_CLAIM_API_500"
    candidate.proposal.impact = "UNVERIFIED_IMPACT_DATA_LOSS"
    candidate.verification = candidate.verification.model_copy(update={
        "verdict": "insufficient", "severity": None, "verified_claim": None,
        "author_comment": None, "question": VerificationQuestion(
            observation="源码返回 `VALUE = 2`。",
            missing_premise="尚未确定此值是否为接口允许的结果。",
            question="请确认接口是否要求保持原返回值。"),
    })
    candidate.question_digest = finding_digest(candidate, run.snapshot)
    run.report.candidates.append(candidate)
    return candidate


def test_sourced_question_follows_confirmed_findings_without_publishing_raw_claim(normalized_run):
    run, artifacts = normalized_run
    candidate = unresolved_candidate(run)
    plan = PublicationPlanner(artifacts).build(run)
    comment = plan.review_comment_markdown
    assert comment.index("### 已确认问题") < comment.index("### 待核实项") < comment.index("### 限制与待办")
    assert "源码返回 `VALUE = 2`" in comment
    assert "尚未确定此值是否为接口允许的结果" in comment
    assert "请确认接口是否要求保持原返回值" in comment
    assert "UNVERIFIED_CLAIM" not in comment and "UNVERIFIED_IMPACT" not in comment
    assert plan.unresolved_questions[0].candidate_id == candidate.id
    assert plan.unresolved_questions[0].evidence_ids
    assert plan.outcome.confirmed_count == plan.outcome.publishable_count == 1
    assert plan.outcome.unresolved_count == 1
    assert len(plan.inline_drafts) == 1 and plan.summary_findings == []
    assert "可进入正常合并评估" not in comment


@pytest.mark.parametrize("failure", [
    "admission", "source_missing", "source_tampered", "wrong_snapshot", "duplicate",
    "location", "no_question", "unverified", "refuted", "pre_existing", "failed", "cancelled",
    "question_tampered", "digest_missing",
])
def test_unvalidated_or_nonpending_questions_cannot_enter_comment(normalized_run, failure):
    run, artifacts = normalized_run
    candidate = unresolved_candidate(run)
    if failure == "admission":
        candidate.admission_reason = "invalid_source_refs"
    elif failure == "source_missing":
        candidate.verification_sources.clear()
    elif failure == "source_tampered":
        candidate.verification_sources[1].content_hash = "0" * 64
    elif failure == "wrong_snapshot":
        run.snapshot.head_sha = "c" * 40
    elif failure == "duplicate":
        run.report.candidates.append(candidate.model_copy(deep=True))
    elif failure == "location":
        candidate.proposal.location.line = 99
    elif failure == "no_question":
        candidate.verification.question = None
    elif failure == "unverified":
        candidate.verification = None
    elif failure == "refuted":
        candidate.verification.verdict = "refuted"
    elif failure == "pre_existing":
        candidate.verification.attribution = "pre_existing"
    elif failure == "question_tampered":
        candidate.verification.question.observation = "UNVERIFIED_UNRELATED_OBSERVATION"
    elif failure == "digest_missing":
        candidate.question_digest = None
    else:
        run.status = failure
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.unresolved_questions == []
    assert "### 待核实项" not in plan.review_comment_markdown
    assert "UNVERIFIED_CLAIM" not in plan.review_comment_markdown
    assert "UNVERIFIED_UNRELATED_OBSERVATION" not in plan.review_comment_markdown


def test_question_markdown_is_escaped_and_bound_to_plan_hash(normalized_run):
    from app.core.fingerprints import stable_hash
    from app.services.publication.publication_worker import compose_summary

    run, artifacts = normalized_run
    candidate = unresolved_candidate(run)
    first = PublicationPlanner(artifacts).build(run)
    candidate.verification.question.question = "@author <script>x</script> [link](javascript:run)\n- injected"
    candidate.question_digest = finding_digest(candidate, run.snapshot)
    second = PublicationPlanner(artifacts).build(run)
    assert stable_hash(first.model_dump(mode="json")) != stable_hash(second.model_dump(mode="json"))
    comment = compose_summary(second, {"finding_keys": []}, set(), [])
    assert "### 待核实项" in comment
    assert "@author" not in comment and "<script>" not in comment
    assert "javascript\\:run" in comment and "\n\\- injected" in comment
    assert "＠author" in comment
    assert "投递状态" not in comment
    assert "Publishing" not in comment
    assert "Inline 失败" not in comment


def test_large_question_summary_is_bounded_without_losing_local_report_details(large_question_run):
    run, artifacts = large_question_run
    plan = PublicationPlanner(artifacts).build(run)
    assert len(plan.review_comment_markdown.encode("utf-8")) <= 57_000
    assert "条待核实项因评论长度限制省略" in plan.review_comment_markdown
    assert "存在 15 个未决问题" in plan.review_comment_markdown
    assert plan.outcome.unresolved_count == len(plan.unresolved_questions) == 15
    assert "可进入正常合并评估" not in plan.review_comment_markdown
    for index in range(15):
        assert f"QUESTION\\_{index}\\:" in plan.user_report_markdown


@pytest.mark.parametrize("observation", ["观察" * 250, "<&@" * 166], ids=["chinese", "escaped"])
def test_question_budget_accounts_for_other_sections_and_escaped_utf8(large_question_run, observation):
    run, artifacts = large_question_run
    plan = PublicationPlanner(artifacts).build(run)
    plan.overview_markdown = "说明" * 6000
    for item in plan.unresolved_questions:
        item.observation = observation
    before = plan.model_dump()
    summary = render_summary(plan)
    shown = summary.count("源码观察：")
    assert len(summary.encode("utf-8")) <= 57_000
    assert 0 < shown < len(plan.unresolved_questions)
    assert f"另有 {15 - shown} 条待核实项因评论长度限制省略" in summary
    assert summary.count("缺失前提：") == summary.count("请确认：") == shown
    assert "未形成通过校验" not in summary
    assert plan.model_dump() == before


def test_question_budget_can_omit_all_questions_without_hiding_unresolved_count(large_question_run):
    run, artifacts = large_question_run
    plan = PublicationPlanner(artifacts).build(run)
    plan.overview_markdown = "说明" * 9000
    summary = render_summary(plan)
    assert len(summary.encode("utf-8")) <= 57_000
    assert "另有 15 条待核实项因评论长度限制省略" in summary
    assert "存在 15 个未决问题" in summary
    assert "源码观察：" not in summary
    assert "未形成通过校验" not in summary


@pytest.mark.parametrize("failure", ["missing", "tampered", "admission", "missing_finding"])
def test_invalid_terminal_candidate_cannot_produce_clean_advice(normalized_run, failure):
    run, artifacts = normalized_run
    run.report.findings.clear()
    candidate = run.report.candidates[0]
    candidate.finding = None
    candidate.verification.verdict = "refuted"
    if failure == "missing":
        candidate.verification_sources.clear()
    elif failure == "tampered":
        candidate.verification_sources[1].content_hash = "0" * 64
    elif failure == "admission":
        candidate.admission_reason = "invalid_source_refs"
    else:
        candidate.verification.verdict = "confirmed"
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.outcome.kind == "inconclusive"
    assert plan.outcome.unresolved_count == 1
    assert "可进入正常合并评估" not in plan.review_comment_markdown


def test_summary_fallback_keeps_the_verifier_comment(normalized_run):
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    finding = plan.inline_drafts[0].model_copy(update={
        "placement": "summary",
        "title": "SUMMARY TITLE ONCE",
        "recommendation": "Use `guard` before dividing.",
    })
    plan.inline_drafts, plan.summary_findings = [], [finding]
    summary = render_summary(plan)
    assert summary.count("SUMMARY TITLE ONCE") == 1
    assert "Use `guard` before dividing\\." in summary
    assert finding.body_markdown not in summary


def test_exact_duplicate_does_not_become_a_false_withheld_gap(normalized_run):
    run, artifacts = normalized_run
    duplicate = run.report.candidates[0].model_copy(deep=True)
    duplicate.id = "another-candidate"
    duplicate.finding.id = "finding-another-candidate"
    duplicate.finding.candidate_id = duplicate.id
    duplicate.finding.admission_digest = finding_digest(duplicate, run.snapshot)
    run.report.candidates.append(duplicate)
    plan = PublicationPlanner(artifacts).build(run)
    assert len(plan.inline_drafts) == 1
    assert plan.withheld_findings == []


def test_withheld_confirmed_result_cannot_produce_clean_conclusion(normalized_run):
    run, artifacts = normalized_run
    run.report.findings[0].admission_digest = "0" * 64
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.withheld_findings
    assert plan.outcome.kind == "inconclusive"


def test_overview_preserves_inline_code_but_escapes_other_markdown():
    rendered = overview_markdown(
        "调整 `weighted_posterior`，避免 <script>alert(1)</script> 和 *伪加粗*。")
    assert rendered == (
        r"调整 `weighted_posterior`，避免 &lt;script&gt;alert\(1\)&lt;/script&gt; "
        r"和 \*伪加粗\*。"
    )


def test_overview_escapes_mentions_links_fences_and_markdown_block_markers():
    text = "@reviewer [link](javascript:alert(1)) ![img](https://example.test)\n- list\n1. item\n```html\n<img>\n```"
    rendered = overview_markdown(text)
    assert "@reviewer" not in rendered
    assert "＠reviewer" in rendered
    assert r"\[link\]\(javascript\:alert\(1\)\)" in rendered
    assert r"\!\[img\]" in rendered
    assert "\n\\- list\n1\\. item" in rendered
    assert "```" not in rendered
    assert "<img>" not in rendered


def test_omission_notice_reaches_comment_and_exact_ranges_remain_in_report(normalized_run):
    from app.review.domain import OverviewOmission
    run, artifacts = normalized_run
    run.report.overview.omitted = {"changes": 1, "sources": 2}
    run.report.overview.omitted_materials = [OverviewOmission(kind="change", unit_id="unit",
        member_id="hunk", path="safe.py", side="head", start_line=1, end_line=3,
        reason="material_capacity")]
    plan = PublicationPlanner(artifacts).build(run)
    assert "总览输入有材料省略" in plan.review_comment_markdown
    assert "safe.py HEAD 1–3：材料容量不足" in plan.user_report_markdown
    assert plan.overview_omitted == {"changes": 1, "sources": 2}


def test_report_leads_with_conclusion_and_moves_file_counts_to_collapsed_details(normalized_run):
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    report = render_user_report(run, plan)

    assert report.index("### 审查结论") < report.index("### 变更总览")
    assert "固定变更" not in report
    assert "Rivu Summary" not in report
    assert "<details>" in report
    assert "变更文件统计（不代表行为总结）" in report
    assert "safe.py: +1 / -1" in report


def test_merge_advice_depends_on_complete_deterministic_review_facts(normalized_run):
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    assert "发现需要处理的已确认问题" in plan.review_comment_markdown
    assert "仍需满足项目测试、CI 和审批要求" not in plan.review_comment_markdown
    assert "可以合并" not in plan.review_comment_markdown

    run.report.findings.clear()
    run.report.candidates.clear()
    clean = PublicationPlanner(artifacts).build(run)
    assert "本轮静态审查未发现需要处理的已确认问题" in clean.review_comment_markdown
    assert "可进入正常合并评估" in clean.review_comment_markdown
    assert "仍需满足项目测试、CI 和审批要求" in clean.review_comment_markdown

    run.status = "partial"
    run.report.status = "partial"
    partial = PublicationPlanner(artifacts).build(run)
    assert "审查结论不完整" in partial.review_comment_markdown
    assert "暂不足以给出明确的合并建议" in partial.review_comment_markdown
    assert "可进入正常合并评估" not in partial.review_comment_markdown


def test_missing_or_inconsistent_report_state_cannot_produce_merge_clearance(normalized_run):
    run, artifacts = normalized_run
    original_report = run.report
    run.report = None
    missing = PublicationPlanner(artifacts).build(run)
    assert missing.outcome.kind == "inconclusive"
    assert "暂不足以给出明确的合并建议" in missing.review_comment_markdown
    assert "可进入正常合并评估" not in missing.review_comment_markdown

    run.report = original_report
    run.status = "completed"
    run.report.status = "partial"
    inconsistent = PublicationPlanner(artifacts).build(run)
    assert inconsistent.outcome.coverage == "partial"
    assert "暂不足以给出明确的合并建议" in inconsistent.review_comment_markdown
    assert "可进入正常合并评估" not in inconsistent.review_comment_markdown


@pytest.mark.parametrize(("status", "expected"), [
    ("failed", "审查未能完成"),
    ("cancelled", "审查已停止"),
])
def test_failed_or_cancelled_review_never_gets_clean_advice(normalized_run, status, expected):
    run, artifacts = normalized_run
    run.status = status
    run.report.status = status
    plan = PublicationPlanner(artifacts).build(run)
    assert expected in plan.review_comment_markdown
    assert "可进入正常合并评估" not in plan.review_comment_markdown
    assert "完整静态审查未发现需要处理的已确认问题" not in plan.review_comment_markdown


def test_withheld_finding_and_unresolved_candidate_never_look_clean(normalized_run):
    run, artifacts = normalized_run
    run.report.findings[0].admission_digest = "0" * 64
    withheld = PublicationPlanner(artifacts).build(run)
    assert withheld.withheld_findings
    assert "审查结论不完整" in withheld.review_comment_markdown
    assert "可进入正常合并评估" not in withheld.review_comment_markdown

    run.report.findings.clear()
    candidate = run.report.candidates[0]
    candidate.finding = None
    candidate.verification = candidate.verification.model_copy(update={"verdict": "insufficient"})
    unresolved = PublicationPlanner(artifacts).build(run)
    assert unresolved.outcome.unresolved_count == 1
    assert "存在 1 个未决问题" in unresolved.review_comment_markdown
    assert "暂不足以给出明确的合并建议" in unresolved.review_comment_markdown


def test_selecting_no_comments_does_not_change_overall_review_conclusion(normalized_run):
    from app.services.publication.publication_worker import compose_summary
    run, artifacts = normalized_run
    plan = PublicationPlanner(artifacts).build(run)
    comment = compose_summary(plan, {"finding_keys": []}, set(), [])
    assert "完整静态审查未发现需要处理的已确认问题" not in comment
    assert "发现需要处理的已确认问题" in comment


@pytest.mark.parametrize("overview_status", ["ready", "failed", "skipped_deadline"])
def test_overview_generation_status_does_not_change_clean_review_advice(normalized_run, overview_status):
    run, artifacts = normalized_run
    run.report.findings.clear()
    run.report.candidates.clear()
    run.report.overview.status = overview_status
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.outcome.kind == "clean"
    assert "可进入正常合并评估" in plan.review_comment_markdown
    assert "仍需满足项目测试、CI 和审批要求" in plan.review_comment_markdown
    assert "投递状态独立于审查结论" not in plan.user_report_markdown


@pytest.mark.parametrize("status", ["queued", "running"])
def test_inflight_review_does_not_prematurely_offer_merge_advice(normalized_run, status):
    run, artifacts = normalized_run
    run.status = status
    run.report.status = status
    plan = PublicationPlanner(artifacts).build(run)
    assert "审查尚未结束，暂不提供合并建议" in plan.review_comment_markdown
    assert "可进入正常合并评估" not in plan.review_comment_markdown


def test_normal_report_does_not_repeat_statistics_or_zero_finding_notice(normalized_run):
    run, artifacts = normalized_run
    run.report.findings.clear()
    run.report.candidates.clear()
    plan = PublicationPlanner(artifacts).build(run)
    plan.overview_markdown = "调整 `ratio` 的输入处理。\n\n补充边界断言。"
    markdown = render_user_report(run, plan)
    assert markdown.startswith("# Rivu 审查评论")
    body, details = markdown.split("<details>")
    assert "safe.py: +1 / -1" not in body
    assert "审查报告版本" not in body
    assert "固定 Head" not in body
    assert "safe.py: +1 / -1" in details
    assert "<summary>审查详情</summary>" in details
    assert "open" not in details
    assert markdown.count("本轮静态审查未发现") == 1
    assert "本轮没有可发布" not in markdown
    assert "### 已确认问题" not in markdown
