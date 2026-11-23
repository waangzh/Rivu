"""远端评论身份校验。"""

from app.tools.github.github_publisher import GitHubPublishError


def validate_comment(comment, context, *, review=False):
    path = f"https://api.github.com/repos/{context['owner']}/{context['repo']}"
    link_key, expected = ("pull_request_url", f"{path}/pulls/{context['pr_number']}") if review else ("issue_url", f"{path}/issues/{context['pr_number']}")
    identity = comment.get("id")
    if (type(identity) is not int or identity < 1 or (comment.get("user") or {}).get("id") != context["publisher_id"]
            or comment.get(link_key) != expected):
        raise GitHubPublishError("remote_object_identity_conflict")
    return comment
