export type Severity = 'critical' | 'high' | 'medium' | 'low';
export type RunStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled';

export interface ReviewLimits {
  concurrency: number | null;
  model_calls: number | null;
  tool_calls: number | null;
  timeout_seconds: number | null;
  output_tokens: number | null;
  total_model_calls: number | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  context_tokens: number | null;
}

export interface ReviewOptions {
  depth: 'quick' | 'standard' | 'deep';
  paths: string[] | null;
  focus: ('correctness' | 'security' | 'performance' | 'compatibility')[];
  include_pr_discussions: boolean;
  generate_overview: boolean;
  gap_followups: number;
  limits: ReviewLimits;
}

export interface Snapshot {
  review_id: string;
  repository: string;
  repository_id: number | null;
  pr_number: number | null;
  declared_base_sha: string;
  base_sha: string;
  head_sha: string;
}

export interface SourceRef {
  id: string;
  review_id: string;
  commit_sha: string;
  side: 'base' | 'head';
  path: string;
  start_line: number;
  end_line: number;
  content_hash: string;
}

export interface DependencyRef {
  id: string;
  review_id: string;
  package: string;
  version: string;
  repository: string;
  commit_sha: string;
  path: string;
  start_line: number;
  end_line: number;
  content_hash: string;
  manifest_source: SourceRef;
  binding: Record<string, string>;
}

export interface Location {
  path: string;
  line: number;
  side: 'base' | 'head';
}

export interface Unit {
  id: string;
  paths: string[];
  required_hunk_ids: string[];
  risk: 'low' | 'medium' | 'high';
  focus: string[];
  estimated_input_tokens: number;
  reused_from: string | null;
  label: string | null;
}

export interface Plan {
  version: string;
  units: Unit[];
  excluded: Record<string, string>;
  incremental_from: string | null;
  history: Record<string, unknown>[];
  incremental_reason: string | null;
  grouping_status: 'pending' | 'deterministic' | 'model' | 'fallback';
  grouping_reason: string | null;
  relations: [number, string, string][];
}

export interface CandidateInput {
  claim: string;
  trigger: string;
  impact: string;
  source_refs: string[];
  location: Location | null;
  root_cause: RootCause | null;
  dependency_refs: string[];
}

export interface RootCause {
  claim: string;
  trigger: string;
  impact: string;
  location: Location;
}

export interface VerificationOutput {
  verdict: 'confirmed' | 'refuted' | 'insufficient';
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  severity: Severity | null;
  reason: string;
  source_refs: string[];
  author_comment: string | null;
  verified_claim: Pick<CandidateInput, 'claim' | 'trigger' | 'impact'> | null;
  root_cause: RootCause | null;
  dependency_refs: string[];
  question: {
    observation: string;
    missing_premise: string;
    question: string;
  } | null;
}

export interface Finding {
  id: string;
  candidate_id: string;
  claim: string;
  trigger: string;
  impact: string;
  severity: Severity;
  attribution: 'introduced' | 'exposed' | 'pre_existing' | 'uncertain';
  author_comment: string;
  location: Location | null;
  sources: SourceRef[];
  publishable: boolean;
  admission_digest: string;
}

export interface Candidate {
  id: string;
  unit_id: string;
  attempt_id: string;
  tool_call_id: string;
  proposal: CandidateInput;
  sources: SourceRef[];
  verification: VerificationOutput | null;
  verification_sources: SourceRef[];
  admission_reason: string | null;
  question_digest: string | null;
  decision_digest: string | null;
  finding: Finding | null;
  previous_candidate_id: string | null;
  dependency_sources: DependencyRef[];
  verification_dependency_sources: DependencyRef[];
}

export interface FindingGroup {
  representative_id: string;
  member_candidate_ids: string[];
}

export interface FindingDeduplication {
  status: 'not_needed' | 'running' | 'ready' | 'failed' | 'skipped';
  input_hash: string | null;
  comparisons: {
    candidate_ids: [string, string];
    admission_digests: [string, string];
    verdict: 'same' | 'distinct' | 'uncertain';
    reason: string;
    source_ids: string[];
  }[];
  omitted_pairs: number;
  error_code: string | null;
}

export interface CompletionGap {
  resolution_scope: 'repository' | 'external';
  required_check: string;
  observation: string;
  missing_premise: string;
  why_required: string;
  source_refs: string[];
}

export interface WorkItemResult {
  [key: string]: unknown;
  unresolved?: CompletionGap | null;
  unresolved_reason?: string | null;
}

export interface WorkItem {
  id: string;
  kind: 'review' | 'verification' | 'supplement';
  followup_of: string | null;
  followup_number: number;
  followup_gap: WorkItemResult | null;
  investigation_deadline: string | null;
  unit_id: string;
  candidate_id: string | null;
  status: 'queued' | 'running' | 'completed' | 'interrupted' | 'failed' | 'skipped';
  attempt: number;
  attempt_id: string | null;
  delivered_hunk_ids: string[];
  visible_source_ids: string[];
  context_archive_uri: string | null;
  delivered_materials: Record<string, unknown>;
  missing_material_reasons: Record<string, string>;
  material_delivery: 'complete' | 'incomplete' | 'not_started';
  error_code: string | null;
  error_diagnostic: {
    ticket: string | null;
    stage: 'request_preparation' | 'transport' | 'response_parsing' | 'agent_processing' | 'execution';
    error_type: string;
    code: string | null;
    http_status: number | null;
    request_id: string | null;
    response_received: boolean;
    usage_known: boolean;
    finish_reason: string | null;
    tool_calls: number;
    reasoning_present: boolean;
  } | null;
  result: WorkItemResult | null;
  tool_calls: number;
  reused_from: string | null;
}

export interface OverviewParagraph {
  text: string;
  source_refs: string[];
}

export interface OverviewOmission {
  kind: 'change' | 'source';
  unit_id: string | null;
  member_id: string | null;
  path: string;
  side: 'base' | 'head' | null;
  start_line: number | null;
  end_line: number | null;
  reason: 'material_capacity' | 'source_unavailable' | 'diff_source_mismatch';
}

export interface Overview {
  status: 'pending' | 'running' | 'ready' | 'failed' | 'skipped_deadline' | 'skipped_cancelled' | 'skipped_empty' | 'skipped_disabled';
  input_hash: string | null;
  output: {
    change_overview: OverviewParagraph[];
    finding_order: string[];
  } | null;
  sources: SourceRef[];
  omitted: Record<'changes' | 'findings' | 'decision_boundaries' | 'sources', number>;
  omitted_materials: OverviewOmission[];
  error_code: string | null;
  diagnostics: {
    attempt: number;
    error_code: 'overview_schema' | 'overview_unknown_source' | 'overview_unknown_finding' | null;
    finish_reason: 'stop' | 'length' | 'tool_calls' | 'content_filter' | 'function_call' | 'other' | null;
    tool_calls: number;
    invalid_tool_calls: number;
    content_present: boolean;
    reasoning_present: boolean;
    validation_errors: number;
    unknown_source_refs: number;
    unknown_finding_ids: number;
  }[];
}

export interface ReviewUsage {
  model_calls?: number;
  unknown_calls?: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_input_tokens?: number | null;
  uncached_input_tokens?: number | null;
  cache_hit_rate?: number | null;
  cache_unknown_calls?: number;
  known_input_tokens?: number;
  known_output_tokens?: number;
  known_cached_input_tokens?: number;
  known_uncached_input_tokens?: number;
  committed_input_tokens?: number;
  committed_output_tokens?: number;
}

export interface ReviewProgress {
  units: WorkItem[];
  usage: ReviewUsage;
}

export interface Report {
  version: string;
  status: RunStatus;
  coverage: 'complete' | 'partial' | 'unknown';
  coverage_reasons: string[];
  unresolved_unit_count: number;
  unresolved_candidate_count: number;
  findings: Finding[];
  candidates: Candidate[];
  finding_groups: FindingGroup[];
  finding_deduplication: FindingDeduplication;
  units: WorkItem[];
  excluded: Record<string, string>;
  overview: Overview;
  usage: ReviewUsage;
  reasons: string[];
}

export interface PullRequestInfo {
  owner: string;
  repo: string;
  number: number;
  title: string;
  body: string | null;
  html_url: string;
}

export interface DiffLine {
  kind: 'added' | 'deleted' | 'context';
  content: string;
  old_line_no: number | null;
  new_line_no: number | null;
}

export interface DiffHunk {
  old_start: number;
  old_length: number;
  new_start: number;
  new_length: number;
  hunk_id: string;
  lines: DiffLine[];
}

export interface ChangedFile {
  file_path: string;
  old_file_path: string | null;
  change_type: string;
  additions: number;
  deletions: number;
  is_binary: boolean;
  hunks: DiffHunk[];
}

export interface LocalGitRequest {
  kind: 'local_git';
  repository_key: string;
  base_ref: string;
  head_ref: string;
  input_id?: string;
  title?: string;
  body?: string;
}

export interface LocalGitSource {
  kind: 'local_git';
  repository_key: string;
  input_id: string;
  input_digest: string;
  title: string;
  body: string;
}

export interface Run {
  dependency_pins: Record<string, Record<string, string>>;
  id: string;
  status: RunStatus;
  phase: string;
  pr_url: string | null;
  pr: PullRequestInfo | null;
  source?: LocalGitSource | null;
  snapshot: Snapshot;
  configuration: {
    provider: string;
    endpoint: string;
    model: string;
    thinking: boolean;
    reasoning_effort: import('./operations').ReasoningEffort | null;
    provider_id: string | null;
    model_profile: import('./operations').ModelProfile | null;
    options: ReviewOptions;
    prompt_version: string;
  };
  changed_files: ChangedFile[];
  plan: Plan;
  report: Report | null;
  discussion_uri: string | null;
  review_cutoff: string;
  hard_deadline: string;
  first_pass_deadline: string | null;
  first_verification_deadline: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_code: string | null;
}

export interface FileContext {
  file_id: number;
  file_path: string;
  snapshot: Snapshot;
  base_lines: string[];
  head_lines: string[];
}

export interface ReviewCreateRequest {
  pr_url?: string;
  source?: LocalGitRequest;
  depth: ReviewOptions['depth'];
  paths: string[] | null;
  focus: ReviewOptions['focus'];
  limits: Partial<ReviewLimits>;
  model?: string;
  provider_id?: string;
  thinking?: boolean;
  reasoning_effort?: import('./operations').ReasoningEffort;
  include_pr_discussions?: boolean;
  generate_overview?: boolean;
  auto_publish?: boolean;
}

export interface ReviewPreviewResponse {
  limits: ReviewLimits;
  source?: LocalGitSource | null;
  snapshot: Snapshot;
  plan: Plan;
  changed_files: ChangedFile[];
}
