"""HTTP boundaries for the single review architecture."""

from typing import Literal

from pydantic import Field, HttpUrl, model_validator

from app.review.domain import Model


class LocalGitRequest(Model):
    kind: Literal["local_git"] = "local_git"
    repository_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    base_ref: str = Field(min_length=1, max_length=200)
    head_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    input_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(default="", max_length=4000)


class ReviewRequest(Model):
    pr_url: HttpUrl | None = None
    source: LocalGitRequest | None = None
    depth: str = "standard"
    paths: list[str] | None = None
    focus: list[str] = Field(default_factory=lambda: [
        "correctness", "security", "performance", "compatibility"])
    limits: dict[str, int] = Field(default_factory=dict, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    include_pr_discussions: bool | None = None
    auto_publish: bool = False

    @model_validator(mode="after")
    def validate_source(self):
        if (self.pr_url is None) == (self.source is None):
            raise ValueError("provide_exactly_one_review_source")
        if self.source and (self.auto_publish or self.include_pr_discussions):
            raise ValueError("local_source_has_no_github_capabilities")
        return self

    def identity_payload(self):
        value = self.model_dump(mode="json")
        if self.source is None:
            value.pop("source")
        if value["paths"] is not None:
            value["paths"] = sorted(set(value["paths"]))
        if not self.auto_publish:
            value.pop("auto_publish")
        return value
