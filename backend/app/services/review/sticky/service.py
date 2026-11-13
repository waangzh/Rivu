"""Sticky Summary 的远端读取、预检与幂等写入。"""

import asyncio

from app.core.fingerprints import stable_hash
from app.tools.github.github_publisher import GitHubPublishError
from app.services.review.sticky.markers import controlled_body, summary_marker, summary_metadata
from app.services.review.sticky.validation import validate_comment


class StickySummary:
    def __init__(self, repository, github, preflight):
        self.repo, self.github, self.preflight = repository, github, preflight

    async def remote(self, context, slot):
        path = self.github.repo_path(context["owner"], context["repo"])
        if slot and slot.github_comment_id:
            try:
                comment = await self.github.get(f"{path}/issues/comments/{slot.github_comment_id}")
            except GitHubPublishError as exc:
                if exc.code != "github_not_found":
                    raise
            else:
                validate_comment(comment, context)
                metadata = summary_metadata(comment.get("body"), context)
                return comment, metadata
        matches = []
        for comment in await self.github.paginate(f"{path}/issues/{context['pr_number']}/comments"):
            if ((comment.get("user") or {}).get("id") == context["publisher_id"]
                    and summary_marker(context) in (comment.get("body") or "")):
                validate_comment(comment, context)
                matches.append((comment, summary_metadata(comment.get("body"), context)))
        if len(matches) > 1:
            raise GitHubPublishError("summary_multiple_matches")
        return matches[0] if matches else (None, None)

    async def preview(self, context):
        slot = await asyncio.to_thread(self.repo.lookup_slot, context["repository_id"], context["pr_number"], context["publisher_identity"])
        if slot and slot.pending_publication_id:
            raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
        comment, metadata = await self.remote(context, slot)
        if metadata and metadata["order"] > context["order"]:
            raise GitHubPublishError("summary_newer_review")
        if comment and slot and slot.last_payload_hash and stable_hash(comment["body"]) != slot.last_payload_hash:
            raise GitHubPublishError("summary_modified_remotely")
        return {"summary_action": "update" if comment else "create", "summary_comment_id": comment["id"] if comment else None}

    async def reconcile(self, attempt, lease):
        """即使 PR 已移动也允许只读对账，避免旧未知请求永久占住 Slot。"""
        slot = await asyncio.to_thread(self.repo.slot, lease)
        if slot.pending_publication_id != attempt.id:
            return
        _, _, context = await asyncio.to_thread(self.repo.frozen, attempt.id)
        item = next(item for item in (await asyncio.to_thread(self.repo.get, attempt.id)).items if item.kind == "summary")
        if item.state not in {"publishing", "uncertain"}:
            return  # 此占位属于 Inline Batch，由批次对账负责释放。
        expected = await asyncio.to_thread(self.repo.item_payload, item.id)
        comment, _ = await self.remote(context, slot)
        if not comment or comment["body"] != expected.get("body"):
            raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
        await asyncio.to_thread(self.repo.summary_finish, attempt, item, comment, comment["body"], context, lease=lease)

    async def upsert(self, attempt, lease, content):
        _, _, context = await asyncio.to_thread(self.repo.frozen, attempt.id)
        slot = await asyncio.to_thread(self.repo.slot, lease)
        if slot.latest_order > context["order"]:
            raise GitHubPublishError("summary_newer_review")
        if slot.pending_publication_id not in {None, attempt.id}:
            raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
        summary = next(item for item in (await asyncio.to_thread(self.repo.get, attempt.id)).items if item.kind == "summary")
        desired = controlled_body(content, attempt, context)
        comment, metadata = await self.remote(context, slot)
        if metadata and metadata["order"] > context["order"]:
            raise GitHubPublishError("summary_newer_review")
        if slot.pending_publication_id == attempt.id:
            previous = await asyncio.to_thread(self.repo.item_payload, summary.id)
            if not comment or comment["body"] != previous.get("body"):
                raise GitHubPublishError("summary_operation_uncertain", uncertain=True)
            await asyncio.to_thread(self.repo.summary_finish, attempt, summary, comment, comment["body"], context, lease=lease)
            slot = await asyncio.to_thread(self.repo.slot, lease)
        if comment and slot.last_payload_hash and stable_hash(comment["body"]) != slot.last_payload_hash:
            raise GitHubPublishError("summary_modified_remotely")
        if comment and comment["body"] == desired:
            await asyncio.to_thread(self.repo.summary_finish, attempt, summary, comment, desired, context, lease=lease)
            return comment
        await self.preflight.validate_current(context, attempt.head_sha)
        await asyncio.to_thread(self.repo.summary_intent, attempt, summary, desired, lease=lease)
        path = self.github.repo_path(context["owner"], context["repo"])
        try:
            result = (await self.github.update_summary(path, comment["id"], desired) if comment else
                      await self.github.create_summary(path, context["pr_number"], desired))
            try:
                validate_comment(result, context)
                if result.get("body") != desired:
                    raise GitHubPublishError("github_response_invalid")
            except GitHubPublishError as exc:
                raise GitHubPublishError(exc.code, uncertain=True) from None
        except GitHubPublishError as exc:
            if not exc.uncertain:
                await asyncio.to_thread(self.repo.summary_rejected, attempt, summary, exc.code, lease=lease)
            raise
        await asyncio.to_thread(self.repo.summary_finish, attempt, summary, result, desired, context, lease=lease)
        # 写前/写后都检查；发现短竞态时保留远端 ID，由调用方标记 stale。
        await self.preflight.validate_current(context, attempt.head_sha)
        return result
