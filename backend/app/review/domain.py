"""The persisted review boundary; agent proposals carry no execution authority."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.paths import validate_repo_relative_path
from app.models.review.diff import ChangedFile, PullRequestInfo

RunStatus = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
PLAN_VERSION = "file-plan-v3"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Limits(Model):
    concurrency: int = Field(default=4, ge=1, le=16)
    model_calls: int = Field(default=48, ge=1, le=100)
    tool_calls: int = Field(default=80, ge=1, le=500)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    output_tokens: int = Field(default=4096, ge=256, le=125_000)
    total_model_calls: int | None = Field(default=None, ge=3, le=1000)
    total_input_tokens: int | None = Field(default=None, ge=12000, le=10_000_000)
    total_output_tokens: int | None = Field(default=None, ge=1024, le=10_000_000)
    context_tokens: int = Field(default=1_000_000, ge=8192, le=1_000_000)


class Options(Model):
    depth: Literal["quick", "standard", "deep"] = "standard"
    paths: list[str] | None = Field(default=None, max_length=2000)
    focus: list[Literal["correctness", "security", "performance", "compatibility"]] = Field(
        default_factory=lambda: ["correctness", "security", "performance", "compatibility"],
        min_length=1, max_length=4)
    include_pr_discussions: bool = False
    generate_overview: bool = True
    gap_followups: int = Field(default=2, ge=0, le=3)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, value):
        return sorted(set(validate_repo_relative_path(path) for path in value)) if value is not None else None


class Configuration(Model):
    provider: str
    endpoint: str
    model: str
    thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] | None = None
    options: Options
    prompt_version: str = "review-vnext-14"


class Snapshot(Model):
    review_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    repository: str
    repository_id: int | None = Field(default=None, gt=0)
    pr_number: int | None = Field(default=None, ge=1)
    declared_base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class SourceRef(Model):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str
    commit_sha: str
    side: Literal["base", "head"]
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class Location(Model):
    path: str
    line: int = Field(ge=1)
    side: Literal["base", "head"] = "head"

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class DependencyRef(Model):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str
    package: str
    version: str
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_source: SourceRef
    binding: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class Unit(Model):
    id: str
    paths: list[str]
    required_hunk_ids: list[str]
    risk: Literal["low", "medium", "high"]
    focus: list[str]
    estimated_input_tokens: int = Field(ge=1)
    reused_from: str | None = None
    label: str | None = Field(default=None, max_length=120)


class Plan(Model):
    version: str = PLAN_VERSION
    units: list[Unit]
    excluded: dict[str, str] = Field(default_factory=dict)
    incremental_from: str | None = None
    history: list[dict] = Field(default_factory=list)
    incremental_reason: str | None = None
    grouping_status: Literal["pending", "deterministic", "model", "fallback"] = "deterministic"
    grouping_reason: str | None = None
    relations: list[tuple[int, str, str]] = Field(default_factory=list)


class FailureDescription(Model):
    claim: str = Field(min_length=1, max_length=1500)
    trigger: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)


class RootCause(FailureDescription):
    location: Location


class CandidateInput(FailureDescription):
    source_refs: list[str] = Field(min_length=1, max_length=32)
    location: Location | None = None
    root_cause: RootCause | None = None
    dependency_refs: list[str] = Field(default_factory=list, max_length=16)


class VerificationQuestion(Model):
    observation: str = Field(min_length=1, max_length=1500)
    missing_premise: str = Field(min_length=1, max_length=1500)
    question: str = Field(min_length=1, max_length=1500)


class VerificationOutput(Model):
    verdict: Literal["confirmed", "refuted", "insufficient"]
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    severity: Literal["critical", "high", "medium", "low"] | None = None
    reason: str = Field(min_length=1, max_length=4000)
    source_refs: list[str] = Field(default_factory=list, max_length=32)
    author_comment: str | None = Field(default=None, max_length=3000)
    verified_claim: FailureDescription | None = None
    question: VerificationQuestion | None = None
    root_cause: RootCause | None = None
    dependency_refs: list[str] = Field(default_factory=list, max_length=16)


class CompletionGap(Model):
    required_check: str = Field(min_length=1, max_length=500)
    observation: str = Field(min_length=1, max_length=1000)
    missing_premise: str = Field(min_length=1, max_length=1000)
    why_required: str = Field(min_length=1, max_length=1000)
    source_refs: list[str] = Field(min_length=1, max_length=32)
    dependency_refs: list[str] = Field(default_factory=list, max_length=16)


class UnitCompletion(Model):
    """Finish this investigation, with a sourced gap only if a necessary check is blocked."""

    unresolved: CompletionGap | None = Field(default=None, description=(
        "Use null when necessary checks are finished, including no defects or only "
        "unsupported speculation. Otherwise identify the required check, sourced observation, "
        "missing fact and why that fact prevents completion. Cite received S source handles. "
        "Summaries, refuted hypotheses and pending candidate verifications are not gaps."
    ))


class Finding(Model):
    id: str
    candidate_id: str
    claim: str
    trigger: str
    impact: str
    severity: Literal["critical", "high", "medium", "low"]
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    author_comment: str
    location: Location | None
    sources: list[SourceRef]
    publishable: bool
    admission_digest: str


class Candidate(Model):
    id: str
    unit_id: str
    attempt_id: str
    tool_call_id: str
    proposal: CandidateInput
    sources: list[SourceRef]
    verification: VerificationOutput | None = None
    verification_sources: list[SourceRef] = Field(default_factory=list)
    admission_reason: str | None = None
    question_digest: str | None = None
    decision_digest: str | None = None
    finding: Finding | None = None
    previous_candidate_id: str | None = None
    dependency_sources: list[DependencyRef] = Field(default_factory=list)
    verification_dependency_sources: list[DependencyRef] = Field(default_factory=list)


class FindingGroup(Model):
    representative_id: str
    member_candidate_ids: list[str]


class FindingComparison(Model):
    candidate_ids: tuple[str, str]
    admission_digests: tuple[str, str]
    verdict: Literal["same", "distinct", "uncertain"]
    reason: str = Field(min_length=1, max_length=1500)
    source_ids: list[str] = Field(min_length=1, max_length=32)


class FindingDeduplication(Model):
    status: Literal["not_needed", "running", "ready", "failed", "skipped"] = "not_needed"
    input_hash: str | None = None
    comparisons: list[FindingComparison] = Field(default_factory=list, max_length=12)
    omitted_pairs: int = Field(default=0, ge=0)
    error_code: str | None = None


class AgentErrorDiagnostic(Model):
    ticket: str | None = None
    stage: Literal["request_preparation", "transport", "response_parsing", "agent_processing",
                   "execution"]
    error_type: str
    code: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    request_id: str | None = None
    response_received: bool = False
    usage_known: bool = False
    finish_reason: str | None = None
    tool_calls: int = Field(default=0, ge=0)
    reasoning_present: bool = False


class WorkItem(Model):
    id: str
    kind: Literal["review", "verification", "supplement"]
    unit_id: str
    candidate_id: str | None = None
    status: Literal["queued", "running", "completed", "interrupted", "failed", "skipped"] = "queued"
    attempt: int = 0
    attempt_id: str | None = None
    delivered_hunk_ids: list[str] = Field(default_factory=list)
    delivered_materials: dict[str, str] = Field(default_factory=dict)
    visible_source_ids: list[str] = Field(default_factory=list)
    context_archive_uri: str | None = None
    missing_material_reasons: dict[str, str] = Field(default_factory=dict)
    material_delivery: Literal["complete", "incomplete", "not_started"] = "not_started"
    error_code: str | None = None
    error_diagnostic: AgentErrorDiagnostic | None = None
    result: dict | None = None
    tool_calls: int = 0
    reused_from: str | None = None
    followup_of: str | None = None
    followup_number: int = 0
    followup_gap: dict | None = None
    investigation_deadline: datetime | None = None

    @property
    def has_unresolved_investigation(self):
        return bool(self.result and (
            self.result.get("unresolved") or self.result.get("unresolved_reason")))


class OverviewParagraph(Model):
    text: str = Field(min_length=1, max_length=1000)
    source_refs: list[str] = Field(min_length=1, max_length=32)


class OverviewOutput(Model):
    change_overview: list[OverviewParagraph] = Field(min_length=1, max_length=5)
    finding_order: list[str] = Field(default_factory=list, max_length=100)


class OverviewOmission(Model):
    kind: Literal["change", "source"]
    unit_id: str | None = None
    member_id: str | None = None
    path: str
    side: Literal["base", "head"] | None = None
    start_line: int | None = None
    end_line: int | None = None
    reason: Literal["material_capacity", "source_unavailable", "diff_source_mismatch"]


class OverviewDiagnostic(Model):
    attempt: int = Field(ge=1, le=2)
    error_code: Literal["overview_schema", "overview_unknown_source", "overview_unknown_finding"] | None = None
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "function_call", "other"] | None = None
    tool_calls: int = Field(ge=0)
    invalid_tool_calls: int = Field(ge=0)
    content_present: bool
    reasoning_present: bool
    validation_errors: int = Field(default=0, ge=0)
    unknown_source_refs: int = Field(default=0, ge=0)
    unknown_finding_ids: int = Field(default=0, ge=0)


class Overview(Model):
    status: Literal["pending", "running", "ready", "failed", "skipped_deadline",
                    "skipped_cancelled", "skipped_empty", "skipped_disabled"] = "pending"
    input_hash: str | None = None
    output: OverviewOutput | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    error_code: str | None = None
    omitted: dict[str, int] = Field(default_factory=dict)
    omitted_materials: list[OverviewOmission] = Field(default_factory=list)
    diagnostics: list[OverviewDiagnostic] = Field(default_factory=list, max_length=2)


class Report(Model):
    version: str
    status: RunStatus
    findings: list[Finding]
    candidates: list[Candidate]
    finding_groups: list[FindingGroup] = Field(default_factory=list)
    finding_deduplication: FindingDeduplication = Field(default_factory=FindingDeduplication)
    units: list[WorkItem]
    excluded: dict[str, str]
    overview: Overview = Field(default_factory=Overview)
    usage: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    coverage_reasons: list[str] = Field(default_factory=list)
    unresolved_unit_count: int = 0
    unresolved_candidate_count: int = 0


class LocalGitSource(Model):
    kind: Literal["local_git"] = "local_git"
    repository_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    input_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=4000)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Run(Model):
    id: str
    status: RunStatus = "queued"
    phase: str = "prepare"
    pr_url: str | None = None
    pr: PullRequestInfo | None = None
    source: LocalGitSource | None = None
    snapshot: Snapshot
    configuration: Configuration
    changed_files: list[ChangedFile]
    plan: Plan
    report: Report | None = None
    review_cutoff: datetime
    hard_deadline: datetime
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    discussion_uri: str | None = None
    dependency_pins: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_source_identity(self):
        if self.source:
            if (self.pr is not None or self.pr_url is not None
                    or self.snapshot.repository_id is not None or self.snapshot.pr_number is not None
                    or self.snapshot.repository != f"local:{self.source.repository_key}"):
                raise ValueError("local_source_identity_mismatch")
        elif (self.pr is None or self.pr_url is None or self.snapshot.repository_id is None
              or self.snapshot.pr_number is None):
            raise ValueError("github_source_requires_pr")
        return self
