import type { ReviewLimits, ReviewOptions } from './review';

export type { ReviewLimits };
export type ReviewPlanOptions = ReviewOptions;

export interface ReviewCapabilities {
  local_repositories?: { key: string; label: string }[];
  depth_presets: {
    id: ReviewPlanOptions['depth'];
    label: string;
    limits: ReviewLimits;
    gap_followups: number;
    grouping_min_files: number | null;
  }[];
  focus_categories: ReviewPlanOptions['focus'];
  limits: {
    defaults: ReviewLimits;
    maxima: ReviewLimits;
  };
  pr_discussions_available?: boolean;
  pr_discussions_default?: boolean;
}
