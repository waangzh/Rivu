"""Sticky Summary 的标记、受控正文与元数据解析。"""

import base64
import json
import re

from app.core.fingerprints import stable_hash
from app.tools.github.github_publisher import GitHubPublishError


def summary_marker(context):
    return (f"<!-- rivu:summary:v1\nrepository_id={context['repository_id']}\n"
            f"pr={context['pr_number']}\npublisher={context['publisher_identity']}\n-->")


def controlled_body(content, attempt, context):
    metadata = {"hash": stable_hash(content), "publication": attempt.id, "review_id": attempt.review_id,
                "head_sha": attempt.head_sha, "order": context["order"]}
    encoded = base64.urlsafe_b64encode(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    body = content + "\n\n" + summary_marker(context) + f"\n<!-- rivu:payload:v1 {encoded} -->"
    if len(body) > 60_000:
        raise GitHubPublishError("publication_body_too_long")
    return body


def summary_metadata(body, context):
    if not isinstance(body, str):
        raise GitHubPublishError("summary_identity_conflict")
    match = re.search(r"\n<!-- rivu:payload:v1 ([A-Za-z0-9_-]+) -->$", body)
    if not match:
        raise GitHubPublishError("summary_identity_conflict")
    try:
        metadata = json.loads(base64.urlsafe_b64decode(match[1] + "=" * (-len(match[1]) % 4)))
        prefix = body[:match.start()]
        suffix = "\n\n" + summary_marker(context)
        if (not prefix.endswith(suffix) or metadata["hash"] != stable_hash(prefix[:-len(suffix)])
                or not all(isinstance(metadata[key], str) for key in ("order", "head_sha", "review_id", "publication"))):
            raise ValueError
        return metadata
    except (ValueError, KeyError, TypeError):
        raise GitHubPublishError("summary_identity_conflict") from None
