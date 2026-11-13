"""独立投递 Worker：冻结选择、逐写复查、持久化批次与保守远端对账。"""
import asyncio
import re
from uuid import uuid4

from app.models.publication import PublicationPlan
from app.core.fingerprints import stable_hash
from app.services.publication.publication_renderer import render_summary
from app.services.publication.publication_repository import PublicationConflict, PublicationLeaseLost, finding_fingerprint
from app.services.review.sticky import StickySummary, validate_comment
from app.services.review.task_queue import ReviewWorker, RetryAfterJobError
from app.tools.github.github_publisher import GitHubPublishError


def batch_marker(publication_id, batch_id):
    return f"<!-- rivu:review-batch:v1\npublication={publication_id}\nbatch={batch_id}\n-->"


def finding_body(finding, publication_id):
    return (finding.body_markdown + f"\n\n<!-- rivu:finding:v1\npublication={publication_id}\n"
            f"finding={finding_fingerprint(finding)}\n-->")


def compose_summary(plan, selection, fallback, items):
    selected = set(selection["finding_keys"])
    inline, summary = [], [item for item in plan.summary_findings if item.finding_key in selected]
    for finding in plan.inline_drafts:
        if finding.finding_key not in selected:
            continue
        if finding.finding_key in fallback:
            summary.append(finding.model_copy(update={"placement": "summary"}))
        else:
            inline.append(finding)
    preview = plan.model_copy(update={"inline_drafts": inline, "summary_findings": summary})
    # 仅接入已经通过远端身份对账并落库的成功回执，不猜测 URL。
    inline_keys = {finding.finding_key for finding in inline}
    receipts = {}
    for item in items:
        key, url = getattr(item, "finding_key", None), getattr(item, "github_url", None)
        if (item.kind == "inline" and item.state == "published" and key in inline_keys
                and isinstance(url, str) and re.fullmatch(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*#discussion(?:_r|-diff-)[0-9]+", url)):
            receipts.setdefault(key, set()).add(url)
    discussions = {key: next(iter(urls)) for key, urls in receipts.items() if len(urls) == 1}
    return render_summary(preview, discussion_urls=discussions)


class PublicationDelivery:
    def __init__(self, repository, github, preflight, task_loader):
        self.repo, self.github, self.preflight, self.task_loader = repository, github, preflight, task_loader
        self.sticky = StickySummary(repository, github, preflight)

    async def comment_details(self, context, comment):
        try:
            validate_comment(comment, context, review=True)
        except GitHubPublishError as exc:
            raise GitHubPublishError(exc.code, uncertain=True) from None
        if comment.get("line") is not None and comment.get("side") is not None:
            return comment
        path = self.github.repo_path(context["owner"], context["repo"])
        try:
            detail = await self.github.get(f"{path}/pulls/comments/{comment['id']}")
        except GitHubPublishError as exc:
            if exc.code == "github_not_found":
                return None
            raise GitHubPublishError(exc.code, uncertain=True, retry_after=exc.retry_after) from None
        try:
            validate_comment(detail, context, review=True)
        except GitHubPublishError as exc:
            raise GitHubPublishError(exc.code, uncertain=True) from None
        fields = ("id", "body", "path", "commit_id", "pull_request_review_id",
                  "pull_request_url", "html_url", "line", "side")
        if any(comment.get(field) is not None and detail.get(field) != comment[field] for field in fields):
            raise GitHubPublishError("inline_identity_conflict", uncertain=True)
        return detail

    async def reconcile_pending(self, context):
        slot = await asyncio.to_thread(self.repo.lookup_slot, context["repository_id"],
                                      context["pr_number"], context["publisher_identity"])
        if slot is None or not slot.pending_publication_id:
            return
        attempt = await asyncio.to_thread(self.repo.get, slot.pending_publication_id)
        if (attempt is None or attempt.repository_id != context["repository_id"]
                or attempt.pr_number != context["pr_number"]
                or attempt.publisher_identity != context["publisher_identity"]):
            raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
        lease = await asyncio.to_thread(self.repo.acquire, attempt, uuid4().hex, reconciliation=True)
        try:
            _, _, frozen_context = await asyncio.to_thread(self.repo.frozen, attempt.id)
            if any(frozen_context.get(field) != context.get(field) for field in
                   ("owner", "repo", "repository_id", "pr_number", "publisher_id", "publisher_identity")):
                raise GitHubPublishError("summary_identity_conflict", uncertain=True)
            await self.sticky.reconcile(attempt, lease)
            await self.recover_batches(attempt, lease, frozen_context)
            current = await asyncio.to_thread(self.repo.get, attempt.id)
            if all(item.state in {"published", "summary_only"} for item in current.items):
                summary = next(item for item in current.items if item.kind == "summary")
                expected = await asyncio.to_thread(self.repo.item_payload, summary.id)
                remote, _ = await self.sticky.remote(frozen_context, await asyncio.to_thread(self.repo.slot, lease))
                if not remote or remote["body"] != expected.get("body"):
                    raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
                await asyncio.to_thread(self.repo.set_state, attempt.id, "succeeded", lease=lease)
        finally:
            await asyncio.to_thread(self.repo.release, lease)

    async def remote_findings(self, context, head_sha, findings, locations):
        if not findings:
            return {}
        path = self.github.repo_path(context["owner"], context["repo"])
        reviews = await self.github.paginate(f"{path}/pulls/{context['pr_number']}/reviews")
        valid_reviews = {}
        for review in reviews:
            if ((review.get("user") or {}).get("id") == context["publisher_id"]
                    and review.get("commit_id") == head_sha and review.get("state") == "COMMENTED"):
                match = re.search(r"<!-- rivu:review-batch:v1\npublication=([a-f0-9]{32})\nbatch=([a-f0-9]{64})\n-->$", review.get("body") or "")
                if match:
                    validate_comment(review, context, review=True)
                    valid_reviews[review["id"]] = match[1]
        matches = {}
        by_fingerprint = {finding_fingerprint(finding): finding for finding in findings}
        for remote in await self.github.paginate(f"{path}/pulls/{context['pr_number']}/comments"):
            if ((remote.get("user") or {}).get("id") != context["publisher_id"] or remote.get("commit_id") != head_sha):
                continue
            match = re.search(r"\n\n<!-- rivu:finding:v1\npublication=([a-f0-9]{32})\nfinding=([a-f0-9]{64})\n-->$", remote.get("body") or "")
            if not match or match[2] not in by_fingerprint:
                continue
            remote = await self.comment_details(context, remote)
            if remote is None:
                raise GitHubPublishError("inline_result_unknown", uncertain=True)
            finding = by_fingerprint[match[2]]
            validate_comment(remote, context, review=True)
            expected = locations.get(finding.finding_key)
            if (valid_reviews.get(remote.get("pull_request_review_id")) != match[1]
                    or remote["body"] != finding_body(finding, match[1]) or not expected
                    or any(remote.get(key) != value for key, value in expected.items())):
                raise GitHubPublishError("inline_identity_conflict", uncertain=True)
            if finding.finding_key in matches:
                raise GitHubPublishError("inline_multiple_matches", uncertain=True)
            matches[finding.finding_key] = remote
        return matches

    async def recover_batches(self, attempt, lease, context):
        path = self.github.repo_path(context["owner"], context["repo"])
        batches = await asyncio.to_thread(self.repo.batches, attempt.id)
        unresolved = [batch for batch in batches if batch["state"] in {"publishing", "uncertain"}]
        if not unresolved:
            return
        reviews = await self.github.paginate(f"{path}/pulls/{context['pr_number']}/reviews")
        for batch in unresolved:
            marker = batch_marker(attempt.id, batch["id"])
            found = [review for review in reviews if (review.get("user") or {}).get("id") == context["publisher_id"]
                     and review.get("commit_id") == attempt.head_sha and (review.get("body") or "").endswith(marker)
                     and review.get("state") == "COMMENTED"]
            receipts = {}
            if len(found) == 1:
                validate_comment(found[0], context, review=True)
                remote = await self.github.paginate(f"{path}/pulls/{context['pr_number']}/reviews/{found[0]['id']}/comments")
                for item_id, expected in zip(batch["item_ids"], batch["payload"]["comments"], strict=True):
                    matching = [comment for comment in remote if comment.get("pull_request_review_id") == found[0]["id"]
                                and comment.get("commit_id") == attempt.head_sha
                                and comment.get("path") == expected["path"] and comment.get("body") == expected["body"]]
                    if len(matching) == 1:
                        detail = await self.comment_details(context, matching[0])
                        if (detail is not None and detail.get("pull_request_review_id") == found[0]["id"]
                                and detail.get("commit_id") == attempt.head_sha
                                and all(detail.get(key) == value for key, value in expected.items())):
                            receipts[item_id] = detail
            complete = len(receipts) == len(batch["item_ids"])
            await asyncio.to_thread(self.repo.finish_batch, batch["id"], receipts, "published" if complete else "uncertain",
                                    None if complete else "batch_result_unknown", lease=lease)
            if not complete:
                raise GitHubPublishError("batch_result_unknown", uncertain=True)

    async def deliver_inline(self, attempt, lease, checked):
        context = checked["context"]
        await self.recover_batches(attempt, lease, context)
        plan = checked["plan"]
        findings = [item for item in plan.inline_drafts if item.finding_key in checked["finding_keys"] and item.finding_key in checked["locations"]]
        remote = await self.remote_findings(context, attempt.head_sha, findings, checked["locations"])
        items = (await asyncio.to_thread(self.repo.get, attempt.id)).items
        pending = []
        by_key = {finding.finding_key: finding for finding in findings}
        for item in items:
            if item.kind != "inline":
                continue
            if item.finding_key in checked["fallback"]:
                await asyncio.to_thread(self.repo.save_item, item.id, lease=lease, state="summary_only", last_error_code=checked["fallback"][item.finding_key])
            elif item.finding_key in remote:
                receipt = remote[item.finding_key]
                await asyncio.to_thread(self.repo.save_item, item.id, lease=lease, state="published", github_comment_id=receipt["id"],
                    github_review_id=receipt["pull_request_review_id"], github_url=receipt["html_url"], last_error_code=None)
            elif item.state in {"published", "publishing", "uncertain"}:
                raise GitHubPublishError("inline_result_unknown", uncertain=True)
            else:
                pending.append(item)
        size = self.preflight.config.rivu_github_publish_batch_size
        path = self.github.repo_path(context["owner"], context["repo"])
        for offset in range(0, len(pending), size):
            batch = pending[offset:offset + size]
            identity = stable_hash([attempt.id, [item.id for item in batch]])
            payload = {"commit_id": attempt.head_sha, "event": "COMMENT", "body":
                "Rivu 已提交本次快照的代码行审查意见；总体审查结论请查看本 PR 的审查摘要。\n\n"
                + batch_marker(attempt.id, identity),
                "comments": [{**checked["locations"][item.finding_key], "body": finding_body(by_key[item.finding_key], attempt.id)} for item in batch]}
            if any(len(comment["body"].encode()) > 60_000 for comment in payload["comments"]):
                raise GitHubPublishError("publication_body_too_long")
            await self.preflight.validate_current(context, attempt.head_sha)
            await asyncio.to_thread(self.repo.prepare_batch, identity, attempt.id, [item.id for item in batch], payload, lease=lease)
            try:
                result = await self.github.create_review(path, context["pr_number"], payload)
                try:
                    validate_comment(result, context, review=True)
                    if result.get("commit_id") != attempt.head_sha or result.get("body") != payload["body"] or result.get("state") != "COMMENTED":
                        raise GitHubPublishError("github_response_invalid")
                except GitHubPublishError as exc:
                    raise GitHubPublishError(exc.code, uncertain=True) from None
            except GitHubPublishError as exc:
                await asyncio.to_thread(self.repo.finish_batch, identity, {}, "uncertain" if exc.uncertain else "failed", exc.code, lease=lease)
                raise
            await self.recover_batches(attempt, lease, context)
            await self.preflight.validate_current(context, attempt.head_sha)

    async def execute(self, job):
        attempt = await asyncio.to_thread(self.repo.get, job.payload.get("publication_id"))
        if attempt is None or attempt.state == "succeeded":
            return
        lease = await asyncio.to_thread(self.repo.acquire, attempt, f"{job.id}:{job.attempts}", job)
        try:
            await asyncio.to_thread(self.repo.set_state, attempt.id, "preparing", lease=lease)
            frozen, selection, context = await asyncio.to_thread(self.repo.frozen, attempt.id)
            plan = PublicationPlan.model_validate(frozen)
            await self.sticky.reconcile(attempt, lease)
            await self.recover_batches(attempt, lease, context)
            task = await asyncio.to_thread(self.task_loader, attempt.review_id)
            if task is None or task.status not in {"completed", "partial"}:
                raise GitHubPublishError("review_not_found")
            if task.snapshot.head_sha != attempt.head_sha:
                raise GitHubPublishError("publication_stale")
            checked = await self.preflight.check(task, selection["finding_keys"])
            if "automatic_authorization" in context:
                from app.services.publication.automatic_publication import validate_automatic_authorization
                grant = validate_automatic_authorization(
                    context["automatic_authorization"], checked["context"], attempt.head_sha)
                if (grant.review_id != task.id or checked["requires_partial_acknowledgement"]
                        or task.status != "completed" or len(checked["plan"].inline_drafts) > grant.max_inline):
                    raise GitHubPublishError("automatic_publication_requires_confirmation")
                checked["context"]["automatic_authorization"] = context["automatic_authorization"]
            if (checked["plan_hash"] != attempt.plan_hash or checked["context"] != context
                    or selection.get("expected_report_version") != checked["report_version"]):
                raise GitHubPublishError("publication_plan_changed")
            if checked["requires_partial_acknowledgement"] and not selection["acknowledge_partial_review"]:
                raise GitHubPublishError("partial_acknowledgement_required")
            # 未知旧请求先恢复，不能先用新的 Publishing 正文覆盖恢复线索。
            await self.recover_batches(attempt, lease, context)
            items = (await asyncio.to_thread(self.repo.get, attempt.id)).items
            await self.sticky.upsert(attempt, lease, compose_summary(plan, selection, checked["fallback"], items))
            await asyncio.to_thread(self.repo.set_state, attempt.id, "publishing", lease=lease)
            await self.deliver_inline(attempt, lease, checked)
            items = (await asyncio.to_thread(self.repo.get, attempt.id)).items
            await self.sticky.upsert(attempt, lease, compose_summary(plan, selection, checked["fallback"], items))
            await asyncio.to_thread(self.repo.set_state, attempt.id, "succeeded", lease=lease)
        except GitHubPublishError as exc:
            items = (await asyncio.to_thread(self.repo.get, attempt.id)).items
            state = ("uncertain" if exc.uncertain else "stale" if exc.code in {"publication_stale", "summary_newer_review"}
                     else "partial" if any(item.state == "published" for item in items) else "failed")
            await asyncio.to_thread(self.repo.set_state, attempt.id, state, exc.code, lease=lease)
            if not exc.uncertain and state == "partial" and not exc.retry_after and "checked" in locals():
                try:
                    items = (await asyncio.to_thread(self.repo.get, attempt.id)).items
                    await self.sticky.upsert(attempt, lease, compose_summary(plan, selection, checked["fallback"], items))
                except GitHubPublishError as final_error:
                    if final_error.uncertain:
                        await asyncio.to_thread(self.repo.set_state, attempt.id, "uncertain", final_error.code, lease=lease)
            if exc.retry_after:
                raise RetryAfterJobError(exc.code, exc.retry_after) from None
        except asyncio.CancelledError:
            # 发送意图已持久化；失租不回写，由新持有者对账。
            raise
        except PublicationLeaseLost:
            raise
        except Exception:
            # DB 提交/进程故障时可能已发出请求，保守保持恢复路径，且不导出异常正文。
            try:
                await asyncio.to_thread(self.repo.set_state, attempt.id, "uncertain", "publication_internal_error", lease=lease)
            except PublicationConflict:
                pass
            raise RuntimeError("publication_internal_error") from None
        finally:
            await asyncio.to_thread(self.repo.release, lease)


class PublicationWorker(ReviewWorker):
    def __init__(self, queue, delivery, automatic_handler=None, **kwargs):
        async def execute(job):
            if job.kind == "automatic_publication":
                await automatic_handler(job)
            else:
                await delivery.execute(job)
        kinds = ("github_publication", "automatic_publication") if automatic_handler else ("github_publication",)
        super().__init__(queue, execute, kinds=kinds, **kwargs)
