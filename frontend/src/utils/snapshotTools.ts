import type { SnapshotSearchHit } from '../types/review';

export const snapshotHitLabel = (hit: SnapshotSearchHit): string =>
  `${hit.path}:${hit.line} ${hit.text}`;

export const parseSnapshotHits = (payload: unknown): SnapshotSearchHit[] => {
  if (!payload || typeof payload !== 'object') return [];
  const hits = (payload as { hits?: unknown }).hits;
  return Array.isArray(hits)
    ? hits.filter((hit): hit is SnapshotSearchHit =>
      !!hit && typeof hit === 'object' && typeof (hit as SnapshotSearchHit).path === 'string')
    : [];
};
