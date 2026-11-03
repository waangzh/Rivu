import type { ReviewCreateRequest } from '../types/review';
import { ApiError } from '../api/client';

export interface PendingReviewCreation {
  key: string;
  input: ReviewCreateRequest;
  startedAt: string;
}

const storageKey = 'rg-review-creation';
const retryableStatuses = new Set([408, 429, 500, 502, 503, 504]);

export function creationWasRejected(error: unknown) {
  return error instanceof ApiError && error.status != null
    && error.status >= 400 && (error.retryable === false
      || (error.status < 500 && !retryableStatuses.has(error.status)));
}

export function savePendingCreation(value: PendingReviewCreation | null) {
  try {
    if (value) localStorage.setItem(storageKey, JSON.stringify(value));
    else localStorage.removeItem(storageKey);
    return true;
  } catch { return false; }
}

export function saveAcceptedReview(id: string) {
  try {
    localStorage.setItem('rg-review-task', id);
    savePendingCreation(null);
  } catch { /* Keep the pending key if storing the accepted ID fails. */ }
}

export function readPendingCreation(): PendingReviewCreation | null {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || 'null');
    const input = value?.input;
    const source = input?.source;
    const validSource = source
      ? source.kind === 'local_git' && typeof source.repository_key === 'string'
        && /^[a-zA-Z0-9_-]{1,64}$/.test(source.repository_key)
        && typeof source.base_ref === 'string' && !!source.base_ref
        && typeof source.head_ref === 'string' && !!source.head_ref
        && (source.input_id === undefined || /^[0-9a-f]{32}$/.test(source.input_id))
        && input.pr_url == null && !input.auto_publish && !input.include_pr_discussions
      : typeof input?.pr_url === 'string'
        && /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(input.pr_url);
    if (typeof value?.key !== 'string' || !value.key || value.key.length > 200
      || typeof value.startedAt !== 'string' || !Number.isFinite(Date.parse(value.startedAt))
      || !validSource
      || !['quick', 'standard', 'deep'].includes(input.depth)
      || !Array.isArray(input.focus) || !input.focus.length
      || !input.focus.every((item: unknown) => ['correctness', 'security', 'performance', 'compatibility'].includes(String(item)))
      || !(input.paths === null || (Array.isArray(input.paths) && input.paths.every((item: unknown) => typeof item === 'string')))
      || !input.limits || typeof input.limits !== 'object' || Array.isArray(input.limits)
      || (input.model !== undefined && typeof input.model !== 'string')
      || (input.include_pr_discussions !== undefined && typeof input.include_pr_discussions !== 'boolean')
      || (input.auto_publish !== undefined && typeof input.auto_publish !== 'boolean')) return null;
    return value;
  } catch { return null; }
}
