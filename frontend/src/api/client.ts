import type {
  Candidate,
  FileContext,
  Finding,
  FindingGroup,
  FindingDeduplication,
  Report,
  Run,
  ReviewCreateRequest,
  ReviewPreviewResponse,
  ReviewProgress,
  WorkItem,
} from '../types/review';
import type { ReviewCapabilities } from '../types/reviewPlan';
import type {
  ModelCatalogResponse,
  ReviewTaskListResponse,
  SystemDiagnostics,
  ProviderRegistryResponse,
  ProviderConfig,
  ProviderPreset,
} from '../types/operations';
import type {
  PublicationAccepted,
  PublicationAttempt,
  PublicationPlan,
  PublicationPreflightResult,
  PublicationSelection,
  AutomaticPublicationStatus,
} from '../types/publication';
import { publicationErrorText } from '../utils/publicationPresentation';
import type { ExportRequest } from '../types/export';

const API_BASE = import.meta.env.VITE_API_BASE ?? '';
const retryableCreateStatuses = new Set([408, 429, 500, 502, 503, 504]);

export class ApiError extends Error {
  constructor(message: string, readonly status: number | null, readonly retryable?: boolean) {
    super(message);
    this.name = 'ApiError';
  }
}

function apiPath(path: string) {
  return API_BASE + path;
}

function reviewPath(id: string, suffix = '') {
  return '/api/reviews/' + encodeURIComponent(id) + suffix;
}

function requestError(payload: unknown, status: number) {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const message = detail.map((issue: { loc: (string | number)[]; msg: string }) => {
      const field = issue.loc.filter(part => part !== 'body').join('.');
      return field + ': ' + issue.msg;
    }).join('; ');
    return new ApiError(message, status);
  }
  const message = typeof detail === 'string'
    ? detail
    : typeof detail === 'object' && detail !== null
      ? String((detail as { message?: string; code?: string }).message
        ?? (detail as { code?: string }).code
        ?? '请求失败 (' + status + ')')
      : '请求失败 (' + status + ')';

  const retryable = typeof detail === 'object' && detail !== null
    ? (detail as { retryable?: unknown }).retryable : undefined;
  return new ApiError(message, status, typeof retryable === 'boolean' ? retryable : undefined);
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiPath(path), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw requestError(payload, response.status);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const getReviewCapabilities = () =>
  request<ReviewCapabilities>('/api/system/review-capabilities');
export const getAvailableModels = () => request<ModelCatalogResponse>('/api/system/models');
export const getProviderModels = (providerId: string) => request<ModelCatalogResponse>(`/api/system/providers/${encodeURIComponent(providerId)}/models`);
export const getModelProviders = () => request<ProviderRegistryResponse>('/api/system/providers');
export const getProviderPresets = () => request<ProviderPreset[]>('/api/system/provider-presets');
export const testModelConnection = (provider: string, model: string) =>
  request<{ status: 'connected' | 'tools_not_returned'; usage: Record<string, unknown> | null }>(
    '/api/system/providers/' + encodeURIComponent(provider) + '/test', {
      method: 'POST', body: JSON.stringify({ model }),
    });
export const getDiagnostics = () => request<SystemDiagnostics>('/api/system/diagnostics');
export const updateSystemSettings = (value: {
  model_base_url?: string;
  model_name?: string;
  retention_days?: number;
  model_providers?: Record<string, ProviderConfig>;
  default_provider?: string;
}) => request<SystemDiagnostics>('/api/system/settings', {
  method: 'PATCH',
  body: JSON.stringify(value),
});
export const getPublicationCapability = (signal?: AbortSignal) =>
  request<import('../types/operations').PublicationCapability>('/api/system/publication-capability', { signal });
export const getAutomaticPublication = (id: string, signal?: AbortSignal) =>
  request<AutomaticPublicationStatus | null>(reviewPath(id, '/automatic-publication'), { signal });
export const getReview = (id: string, signal?: AbortSignal) =>
  // Report.usage includes provider cache details; absent details remain unknown.
  request<Run>(reviewPath(id), { signal });

export function subscribeToReview(id: string, refresh: () => void) {
  const stream = new EventSource(apiPath(reviewPath(id, '/events')));
  for (const event of ['task', 'progress', 'done']) stream.addEventListener(event, refresh);
  stream.onerror = () => stream.close();
  return () => stream.close();
}

export const listReviews = (query: string, page: number, signal?: AbortSignal) =>
  request<ReviewTaskListResponse>(
    '/api/reviews?q=' + encodeURIComponent(query) + '&page=' + page + '&page_size=20',
    { signal },
  );
export const deleteReview = (id: string) => request<void>(reviewPath(id), { method: 'DELETE' });
export const cancelReview = (id: string) =>
  request<{ status: string }>(reviewPath(id, '/cancel'), { method: 'POST' });
export const retryReview = (id: string) => request<Run>(reviewPath(id, '/retry'), { method: 'POST' });
export const retryOverview = (id: string) =>
  request<Run>(reviewPath(id, '/overview/retry'), { method: 'POST' });

function reviewBody(input: ReviewCreateRequest) {
  return {
    pr_url: input.pr_url,
    ...(input.source ? { source: input.source } : {}),
    depth: input.depth,
    paths: input.paths,
    focus: input.focus,
    limits: Object.fromEntries(
      Object.entries(input.limits).filter(([, value]) => value != null),
    ),
    ...(input.model ? { model: input.model } : {}),
    ...(input.provider_id ? { provider_id: input.provider_id } : {}),
    ...(input.thinking === undefined ? {} : { thinking: input.thinking }),
    ...(input.reasoning_effort ? { reasoning_effort: input.reasoning_effort } : {}),
    ...(input.include_pr_discussions === undefined
      ? {}
      : { include_pr_discussions: input.include_pr_discussions }),
    ...(input.generate_overview === undefined ? {} : { generate_overview: input.generate_overview }),
    ...(input.auto_publish === undefined ? {} : { auto_publish: input.auto_publish }),
  };
}

function aborted(signal: AbortSignal | undefined, error: unknown) {
  return signal?.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

function retryableCreateError(error: unknown) {
  return !(error instanceof ApiError)
    || (error.retryable !== false && retryableCreateStatuses.has(error.status ?? 0));
}

export async function createReview(
  input: ReviewCreateRequest,
  signal?: AbortSignal,
  key: string = crypto.randomUUID(),
  operatorToken?: string,
): Promise<Run> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort', cancel, { once: true });
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Idempotency-Key': key,
      ...(input.auto_publish && operatorToken ? { Authorization: 'Bearer ' + operatorToken } : {}) },
    body: JSON.stringify(reviewBody(input)),
    signal: controller.signal,
  };

  try {
    try {
      return await request<Run>('/api/reviews', init);
    } catch (error) {
      if (aborted(controller.signal, error) || !retryableCreateError(error)) throw error;
      return await request<Run>('/api/reviews', init);
    }
  } finally {
    signal?.removeEventListener('abort', cancel);
  }
}

export const previewReview = (input: ReviewCreateRequest, signal?: AbortSignal) =>
  // Preview is deterministic; semantic grouping is frozen by the review worker.
  request<ReviewPreviewResponse>('/api/reviews/preview', {
    method: 'POST',
    body: JSON.stringify(reviewBody(input)),
    signal,
  });
export const getUnits = (id: string, signal?: AbortSignal) =>
  request<WorkItem[]>(reviewPath(id, '/units'), { signal });
export const getProgress = (id: string, signal?: AbortSignal) =>
  request<ReviewProgress>(reviewPath(id, '/progress'), { signal });
export const getFindings = (id: string, signal?: AbortSignal) =>
  request<{
    findings: Finding[];
    candidates: Candidate[];
    finding_groups: FindingGroup[];
    finding_deduplication: FindingDeduplication | null;
    report_version: string | null;
  }>(reviewPath(id, '/findings'), { signal });
export const getReport = (id: string, signal?: AbortSignal) =>
  request<{ report: Report | null; markdown: string }>(reviewPath(id, '/report'), { signal });
export const getFileContext = (id: string, file: number, signal?: AbortSignal) =>
  request<FileContext>(reviewPath(id, '/files/' + file + '/context'), { signal });

async function publicationRequest<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiPath(path), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + token,
      ...init.headers,
    },
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = typeof payload?.detail?.code === 'string'
      ? payload.detail.code
      : 'HTTP ' + response.status;
    throw new Error(publicationErrorText(code));
  }

  return response.json() as Promise<T>;
}

export const preflightPublication = (reviewId: string, keys: string[], token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationPreflightResult>(reviewPath(reviewId, '/publication-preflight'), token, {
    method: 'POST',
    body: JSON.stringify({ finding_keys: keys }),
    signal,
  });
export const createPublication = (reviewId: string, selection: PublicationSelection, key: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAccepted>(reviewPath(reviewId, '/publications'), token, {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(selection),
    signal,
  });
export const getPublications = (reviewId: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt[]>(reviewPath(reviewId, '/publications'), token, { signal });
export const getPublication = (reviewId: string, identity: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt>(
    reviewPath(reviewId, '/publications/' + encodeURIComponent(identity)),
    token,
    { signal },
  );
export const retryPublication = (reviewId: string, identity: string, token: string, signal?: AbortSignal) =>
  publicationRequest<PublicationAttempt>(
    reviewPath(reviewId, '/publications/' + encodeURIComponent(identity) + '/retry'),
    token,
    { method: 'POST', signal },
  );
export const getPublicationPlan = (taskId: string, signal?: AbortSignal) =>
  request<PublicationPlan>(reviewPath(taskId, '/publication-plan'), { signal });
export const checkReviewComment = (
  taskId: string,
  signal?: AbortSignal,
  options?: import('../types/publication').ReviewCommentOptions,
) => request<import('../types/publication').ReviewCommentCheck>(reviewPath(taskId, '/review-comment-check'), {
  method: 'POST',
  signal,
  body: JSON.stringify(options),
});

export async function exportReview(id: string, input: ExportRequest, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(apiPath(reviewPath(id, '/export')), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
    signal,
  });
  if (!response.ok) {
    throw requestError(await response.json().catch(() => null), response.status);
  }
  return response.blob();
}
