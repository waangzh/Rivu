"""Thin formatting for Publisher-validated findings and unresolved questions."""

import html
import re


# Leave space for delivery status and Publisher metadata below the comment limits.
SUMMARY_CONTENT_BYTES = 57_000


def markdown_text(value, limit=4000):
    text = html.escape(value[:limit], quote=False).replace("@", "＠")
    return re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", text)


def overview_markdown(value, limit=1000):
    """Preserve only single-backtick code; escape all other Markdown and HTML."""
    def prose(text):
        return re.sub(r"([+\-.:])", r"\\\1", markdown_text(text, limit))

    text, parts, start = value[:limit].replace("\r\n", "\n").replace("\r", "\n"), [], 0
    for match in re.finditer(r"(?<!`)`([^`\r\n]+)`(?!`)", text):
        parts.extend((prose(text[start:match.start()]),
                      "`" + match[1].replace("@", "＠") + "`"))
        start = match.end()
    parts.append(prose(text[start:]))
    return "".join(parts)


def _findings(plan):
    return [*plan.inline_drafts, *plan.summary_findings]


def _review_conclusion(plan):
    """Static review advice is determined by facts, never by editorial prose."""
    outcome = plan.outcome
    if outcome.kind == "failed":
        return "审查未能完成，尚不足以判断是否适合合并。已保存的结果仅供后续核查。"
    if outcome.kind == "cancelled":
        return "审查已停止，未形成完整结论。请先完成审查，再评估是否合并。"
    if plan.digest.execution_status in {"queued", "running"}:
        return "审查尚未结束，暂不提供合并建议。"
    incomplete = (outcome.coverage != "complete" or outcome.unresolved_count
                  or outcome.kind == "inconclusive" or plan.withheld_findings
                  or plan.scope_reasons or plan.blocking_reasons)
    if incomplete:
        prefix = ("本轮存在已确认问题，且"
                  if outcome.confirmed_count or outcome.publishable_count else "")
        unresolved = (f"存在 {outcome.unresolved_count} 个未决问题。" if outcome.unresolved_count else "")
        return (prefix + "审查结论不完整，暂不足以给出明确的合并建议。"
                + unresolved + "请先处理已列出的问题、未决项或审查缺口。")
    if outcome.confirmed_count or outcome.publishable_count:
        return "发现需要处理的已确认问题，建议先审阅并处理，再评估是否合并。"
    if outcome.kind != "clean":
        return "当前结果尚不足以给出明确的合并建议，请先核对审查状态与证据。"
    return ("本轮静态审查未发现需要处理的已确认问题，可进入正常合并评估；"
            "仍需满足项目测试、CI 和审批要求。")


def render_finding(finding):
    return finding.body_markdown


def _indented_markdown(value):
    return "\n".join(f"  {line}" if line else "  " for line in value.splitlines())


def _scope_reason(reason):
    if reason == "unverified_historical_candidates":
        return "仍有增量复核项未完成。"
    return markdown_text(reason, 500)


def _question_section(questions, byte_budget):
    header = "\n".join(["", "### 待核实项", "",
                       "以下观察仍缺少必要前提，未计入已确认问题或严重度统计。", ""])
    blocks = []
    for index, item in enumerate(questions, 1):
        location = (f"{item.anchor.file_path}:{item.anchor.start_line} · {item.anchor.side.upper()}"
                    if item.anchor.start_line and item.anchor.side else item.anchor.file_path)
        blocks.append("\n".join([
            f"**{index}. {markdown_text(location, 600) or '位置未确定'}**", "",
            f"源码观察：{overview_markdown(item.observation, 1500)}", "",
            f"缺失前提：{overview_markdown(item.missing_premise, 1500)}", "",
            f"请确认：{overview_markdown(item.question, 1500)}", ""]))
    sizes = [len(block.encode("utf-8")) + 1 for block in blocks]
    size = len(header.encode("utf-8")) + sum(sizes)
    if byte_budget is None or size <= byte_budget:
        return "\n".join([header, *blocks])
    kept = len(blocks)
    while True:
        notice = (f"另有 {len(questions) - kept} 条待核实项因评论长度限制省略，"
                  "完整内容见本地报告；这些项目仍计入未决总数。")
        if kept == 0 or size + len(notice.encode("utf-8")) + 2 <= byte_budget:
            return "\n".join([header, *blocks[:kept], notice, ""])
        kept -= 1
        size -= sizes[kept]


def render_summary(plan, *, discussion_urls=None, all_questions=False):
    findings = _findings(plan)
    lines = ["### 审查结论", "", _review_conclusion(plan), "", "### 变更总览", ""]
    if plan.overview_markdown:
        lines += [plan.overview_markdown, ""]
    else:
        status = {
            "pending": "变更总览尚未生成。", "running": "变更总览正在生成。",
            "skipped_deadline": "已到截止时间，未生成变更总览。",
            "skipped_cancelled": "审查已停止，未生成变更总览。",
            "skipped_empty": "没有可审查范围，未生成变更总览。",
        }.get(plan.overview_status, "变更总览未能生成，已有审查事实仍保留。")
        lines += [status, ""]
    if any(plan.overview_omitted.values()):
        lines += ["总览输入有材料省略，仅概括已提供的变更，不代表完整变更范围。", ""]
    if findings:
        lines += ["### 已确认问题", ""]
        for finding in findings:
            location = (f"（{finding.anchor.file_path}:{finding.anchor.start_line}）"
                        if finding.anchor.start_line else "")
            lines += [f"- **{finding.severity.title()}** {overview_markdown(finding.title, 300)} "
                      f"{markdown_text(location, 600)}"]
            if finding.placement == "summary":
                lines += ["", _indented_markdown(overview_markdown(finding.recommendation, 3000)), ""]
            url = (discussion_urls or {}).get(finding.finding_key)
            if isinstance(url, str) and re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*#discussion_r[0-9]+", url):
                lines.append(f"  [查看代码讨论]({url})")
    question_position = len(lines)
    if plan.outcome.coverage != "complete" or plan.scope_reasons or plan.outcome.unresolved_count:
        lines += ["", "### 限制与待办", ""]
        if plan.outcome.coverage != "complete":
            lines += ["审查范围尚未完整处理，请先补查未完成的部分。", ""]
        if plan.outcome.unresolved_count:
            lines += [f"存在 {plan.outcome.unresolved_count} 个未决问题，需补齐证据或必要前提。", ""]
            remaining = plan.outcome.unresolved_count - len(plan.unresolved_questions)
            if remaining > 0:
                lines += [f"其中 {remaining} 项尚未形成通过校验的待核实内容，"
                          "请在本地报告查看复核状态与证据校验说明。", ""]
        lines += [f"- {_scope_reason(reason)}" for reason in plan.scope_reasons[:100]]
    if plan.withheld_findings:
        lines += ["", "### 未发布记录", ""]
        lines += [f"- `{item.candidate_id}`：{item.reason_code}" for item in plan.withheld_findings]
    lines += ["", "本轮仅进行静态审查，未执行目标仓库代码、测试或构建。", "",
              "<details>", "<summary>审查详情</summary>", "",
              f"审查报告版本：`{plan.report_version}`", "",
              f"固定 Head：`{plan.snapshot.head_sha or 'unknown'}`"]
    if plan.digest.actual_changes:
        lines += ["", "变更文件统计（不代表行为总结）：", "",
                  *[f"- {markdown_text(item, 500)}" for item in plan.digest.actual_changes[:100]]]
    lines += ["", "</details>"]
    if plan.unresolved_questions:
        byte_budget = (None if all_questions else
                       max(0, SUMMARY_CONTENT_BYTES - len("\n".join(lines).encode("utf-8")) - 1))
        lines.insert(question_position, _question_section(plan.unresolved_questions, byte_budget))
    return "\n".join(lines)


def render_review_comment(plan, *, detail="compact", exclude_resolved=False):
    return render_summary(plan)


def render_finding_comment(plan, finding):
    return finding.body_markdown


def comment_selection(plan, *, exclude_resolved=False):
    return _findings(plan), [], []


def render_user_report(run, plan):
    lines = ["# Rivu 审查报告" if run.source else "# Rivu 审查评论", "", render_summary(plan, all_questions=True)]
    if run.report and run.report.overview.omitted_materials:
        lines += ["", "### 总览省略范围", ""]
        reasons = {"material_capacity": "材料容量不足", "source_unavailable": "源码不可用",
                   "diff_source_mismatch": "Diff 与源码不一致"}
        for item in run.report.overview.omitted_materials:
            location = f" {item.side.upper()} {item.start_line}–{item.end_line}" if item.side else ""
            lines.append(f"- {markdown_text(item.path, 500)}{location}：{reasons[item.reason]}")
    return "\n".join(lines)


def discussion_report_lines(plan, task=None):
    return []
