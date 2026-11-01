import { ApiError } from '../api/client';
import type { Candidate, Run, WorkItem } from '../types/review';
import { terminal } from './nativeReview';

export function initialReviewSection(status: Run['status']) {
  return terminal(status) ? 'overview' : 'activity';
}

export function reviewRecordMissing(error: unknown) {
  return error instanceof ApiError && error.status === 404;
}

export function formatElapsed(start: string, end: string | number) {
  const startTime = Date.parse(start);
  const endTime = typeof end === 'number' ? end : Date.parse(end);
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) return '—';
  const seconds = Math.max(0, Math.floor((endTime - startTime) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes} 分 ${seconds % 60} 秒`
    : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

export function progressSummary(task: Run, units: WorkItem[], candidates: Candidate[]) {
  const review = units.filter(item => item.kind === 'review');
  const completed = new Set(review.filter(item => item.status === 'completed').map(item => item.unit_id));
  const unavailable = new Set(review.filter(item =>
    ['failed', 'interrupted', 'skipped'].includes(item.status),
  ).map(item => item.unit_id));
  const pending = candidates.filter(item => !item.verification).length;
  const insufficient = candidates.filter(item => item.verification?.verdict === 'insufficient').length;
  return {
    total: task.plan.grouping_status === 'pending' ? null : task.plan.units.length,
    completed: completed.size,
    unavailable: unavailable.size,
    reviewing: review.filter(item => item.status === 'running').length,
    verifying: units.filter(item => item.kind === 'verification' && item.status === 'running').length,
    pending,
    insufficient,
  };
}

export function progressHeading(task: Run) {
  if (task.status === 'cancelled') return '审查已停止';
  if (task.status === 'failed') return '审查未能完成';
  if (task.status === 'partial') return '审查已结束，部分审查单元未完成';
  if (task.status === 'completed') return '审查已结束';
  if (task.status === 'queued') return '等待后台处理';
  if (task.phase === 'overview' || task.phase === 'report') return '正在整理审查结果';
  if (task.phase === 'grouping' || task.phase === 'planning') return '正在确定审查单元';
  return '正在分析代码与核验问题';
}
