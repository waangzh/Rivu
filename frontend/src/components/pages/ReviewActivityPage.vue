<script setup lang="ts">
import { computed } from 'vue';
import type { Candidate, Report, ReviewUsage, Run, WorkItem } from '../../types/review';
import type { ReviewSection } from '../../types/workspace';
import { executionErrorLabel, executionStageLabel, hasUnresolvedInvestigation, observedUsage, phaseLabel, reviewReasonLabel, terminal } from '../../utils/nativeReview';
import { formatElapsed, progressHeading, progressSummary } from '../../utils/reviewProgress';
import { useReviewClock } from '../../composables/useReviewClock';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';

const props = withDefaults(defineProps<{
  task: Run;
  units: WorkItem[];
  report: Report | null;
  usage?: ReviewUsage;
  candidates?: Candidate[];
  findingsCount?: number;
  loaded?: boolean;
  findingsLoaded?: boolean;
  syncedAt?: number | null;
  syncError?: string;
}>(), { loaded: true, findingsLoaded: true });
defineEmits<{ navigate: [value: ReviewSection]; refresh: [] }>();
const now = useReviewClock();
const ended = computed(() => terminal(props.task.status));
const usage = computed(() => observedUsage(props.usage ? { usage: props.usage } : props.report));
const summary = computed(() => progressSummary(props.task, props.units, props.candidates ?? props.report?.candidates ?? []));
const elapsed = computed(() => formatElapsed(props.task.created_at,
  ended.value ? props.task.completed_at || props.task.updated_at : now.value));
const syncLabel = computed(() => {
  if (!props.syncedAt) return '等待首次同步';
  const seconds = Math.max(0, Math.floor((now.value - props.syncedAt) / 1000));
  return seconds < 5 ? '刚刚成功同步' : `${formatElapsed(new Date(props.syncedAt).toISOString(), now.value)}前同步`;
});
const preparing = computed(() => ['prepare', 'grouping', 'planning'].includes(props.task.phase));
const reporting = computed(() => ['overview', 'report'].includes(props.task.phase));
const active = computed(() => props.units.filter(item => item.status === 'running'));
const unitOrder = computed(() => new Map(props.task.plan.units.map((item, index) => [item.id, index])));
const ordered = computed(() => {
  const priority: Record<string, number> = { running: 0, queued: 1, failed: 2, interrupted: 2, skipped: 3, completed: 4 };
  return [...props.units].sort((a, b) => priority[a.status] - priority[b.status]
    || (unitOrder.value.get(a.unit_id) ?? 0) - (unitOrder.value.get(b.unit_id) ?? 0)
    || a.id.localeCompare(b.id));
});
const materialLabel = { complete: '所需源码材料已提供', incomplete: '部分源码材料缺失', not_started: '尚未提供源码材料' };
const workLabel = { queued: '等待处理', running: '处理中', completed: '已完成', interrupted: '未完成', failed: '处理失败', skipped: '未执行' };
const groupingLabel = { pending: '等待正式语义分组', deterministic: '确定性分组', model: '语义分组已冻结', fallback: '分组降级为逐文件审查' };
const unitDescription = (id: string) => {
  const unit = props.task.plan.units.find(item => item.id === id);
  return unit?.label || unit?.paths.join('、') || '审查单元';
};
const workPaths = (item: WorkItem) => {
  const unit = props.task.plan.units.find(entry => entry.id === item.unit_id);
  if (!unit) return '';
  // 审查单元没有标签时标题已经显示路径，不再重复一行。
  return item.kind === 'verification' || unit.label ? unit.paths.join(' · ') : '';
};
const workDescription = (item: WorkItem) => item.kind === 'verification'
  ? (props.candidates ?? props.report?.candidates)?.find(candidate => candidate.id === item.candidate_id)?.proposal.claim || unitDescription(item.unit_id)
  : unitDescription(item.unit_id);
const count = (value: number | null) => value == null ? '未知' : value.toLocaleString('zh-CN');
const findingsCount = computed(() => props.findingsCount ?? props.report?.findings?.length);
</script>

<template>
  <section class="progress-summary card card-pad" aria-labelledby="progress-title">
    <div class="progress-eyebrow"><span v-if="!ended && !syncError" class="rg-live-dot" />审查进度</div>
    <h2 id="progress-title" aria-live="polite">{{ progressHeading(task) }}</h2>
    <p class="progress-lead">{{ syncError ? '连接或进度同步出现异常，以下为最近取得的记录。' : ended ? '本轮处理已结束。请结合已确认问题、未完成审查单元和报告限制查看结果。' : task.status === 'queued' ? '任务已创建，等待后台继续处理。你可以离开页面，稍后从审查历史返回。' : reporting ? '代码分析阶段已结束，正在整理变更说明与审查结果。' : preparing ? '正在准备后台审查环境或确定审查单元，暂时没有可展示的分析结果。' : '系统正在读取固定版本源码，分析变更，并独立核验发现的疑似问题。' }}</p>
    <div class="progress-facts">
      <span><AppIcon name="clock" />{{ ended ? '总用时' : '已用时' }} {{ elapsed }}</span>
      <span v-if="!ended"><AppIcon :name="syncError ? 'alert' : 'server'" />{{ syncLabel }}</span>
      <span>{{ task.changed_files.length }} 个变更文件</span>
    </div>
    <p v-if="syncError" class="notice warn space-top" role="alert">{{ syncError }} <button class="link" @click="$emit('refresh')">重试读取进度</button></p>
    <div class="progress-actions space-top">
      <button v-if="ended" class="btn primary" @click="$emit('navigate', 'overview')">查看审查结果<AppIcon name="arrow-right" /></button>
      <button class="btn secondary" @click="$emit('navigate', 'changes')">查看变更</button>
      <button class="btn secondary" @click="$emit('navigate', 'findings')">{{ findingsCount ? `查看已确认问题 (${findingsCount})` : '查看问题与核验' }}</button>
    </div>
  </section>

  <div class="progress-columns space-top">
    <section class="card card-pad">
      <div class="card-header"><h3>审查单元进展</h3><span class="muted small">代码分析</span></div>
      <p v-if="!loaded" class="muted" role="status">正在读取审查单元…</p>
      <template v-else>
        <p v-if="summary.total === null" class="progress-scope-count">正在确定审查单元</p>
        <p v-else-if="!summary.total" class="progress-scope-count">没有可审查单元</p>
        <p v-else class="progress-scope-count">已完成 <strong>{{ summary.completed }}</strong><span> / {{ summary.total }} 个审查单元</span></p>
        <p class="muted small">审查单元数量只反映代码分析进展，不代表整体完成比例或剩余时间。</p>
        <div class="progress-counts">
          <div v-if="!ended"><strong>{{ summary.reviewing }}</strong><span>正在分析</span></div>
          <div v-if="!ended"><strong>{{ summary.verifying }}</strong><span>正在核验</span></div>
          <div v-if="summary.supplementing"><strong>{{ summary.supplementing }}</strong><span>正在补查</span></div>
          <div><strong>{{ summary.unavailable }}</strong><span>未完成审查单元</span></div>
          <div><strong>{{ summary.unresolved }}</strong><span>关键检查仍待核实</span></div>
        </div>
        <p v-if="summary.unavailable" class="notice warn space-top">部分审查单元未完成，相关代码不能视为已通过审查。可在下方执行记录中查看原因。</p>
      </template>
    </section>
    <section class="card card-pad">
      <div class="card-header"><h3>问题核验</h3><AppIcon name="shield" /></div>
      <p v-if="!findingsLoaded || !loaded" class="muted" role="status">正在读取核验状态…</p>
      <template v-else>
        <p class="progress-result">{{ findingsCount == null ? '已确认问题数量未知' : findingsCount ? `已确认 ${findingsCount} 个问题` : ended ? '本轮未记录已确认问题' : '暂未确认问题' }}</p>
        <p class="muted small">疑似问题须经过源码证据检查与独立核验，才会计入已确认问题。</p>
        <div class="progress-counts">
          <div><strong>{{ summary.pending }}</strong><span>等待或正在核验</span></div>
          <div><strong>{{ summary.insufficient }}</strong><span>证据不足，尚未解决</span></div>
        </div>
        <p v-if="!findingsCount" class="muted small space-top">{{ ended ? '未确认问题不代表代码没有缺陷，请同时查看审查单元与限制。' : '审查仍在进行，当前数量不代表最终结论。' }}</p>
      </template>
    </section>
  </div>

  <section class="card card-pad space-top">
    <h3>审查过程</h3>
    <ol class="progress-stages">
      <li data-state="done"><AppIcon name="check-circle" /><div><strong>固定 PR 版本</strong><p>本轮分析始终基于同一份源码快照。</p></div><span>已完成</span></li>
      <li :data-state="task.plan.grouping_status === 'pending' ? ended ? 'stopped' : 'current' : 'done'"><AppIcon :name="task.plan.grouping_status === 'pending' ? 'units' : 'check-circle'" /><div><strong>确定审查单元</strong><p>{{ task.plan.grouping_status === 'pending' ? ended ? '本轮已结束，审查单元尚未确定。' : '正在组织相关文件，审查单元数量尚未确定。' : '已按变更内容组织需要调查的审查单元。' }}</p></div><span>{{ task.plan.grouping_status === 'pending' ? ended ? '未完成' : '处理中' : '已完成' }}</span></li>
      <li :data-state="!ended && !preparing && !reporting ? 'current' : 'neutral'"><AppIcon name="code" /><div><strong>分析代码与核验问题</strong><p>两个过程可以交错进行；发现疑似问题后会独立核验。</p></div><span>{{ ended ? '已结束' : reporting ? '已结束' : preparing ? '等待处理' : '处理中' }}</span></li>
      <li :data-state="reporting && !ended ? 'current' : 'neutral'"><AppIcon name="report" /><div><strong>整理审查结果</strong><p>汇总已确认问题、未完成审查单元和变更说明。</p></div><span>{{ ended ? '已结束' : reporting ? '处理中' : '等待处理' }}</span></li>
    </ol>
  </section>

  <section v-if="!ended" class="card card-pad space-top">
    <div class="card-header"><h3>当前处理</h3><span class="muted small">可同时处理多个审查单元</span></div>
    <p v-if="!loaded" class="muted" role="status">正在读取执行记录…</p>
    <ul v-else-if="active.length" class="progress-current">
      <li v-for="item in active" :key="item.id"><AppIcon :name="item.kind === 'verification' ? 'shield' : 'code'" /><div><strong>{{ workDescription(item) }}</strong><p v-if="workPaths(item)">{{ workPaths(item) }}</p></div><span>{{ item.kind === 'supplement' ? '补查缺口中' : item.kind === 'review' ? '分析中' : '核验疑似问题中' }}</span></li>
    </ul>
    <p v-else class="muted">{{ reporting ? '正在整理结果，暂时没有分析或核验任务。' : preparing ? '尚在准备环境或组织审查单元，分析记录将在开始处理后显示。' : '暂未记录正在处理的审查单元，系统会继续同步后台状态。' }}</p>
  </section>

  <details class="card card-pad progress-disclosure space-top">
    <summary>所有审查单元与核验记录 <span>{{ loaded ? `${ordered.length} 项` : '读取中' }}</span></summary>
    <p v-if="!loaded" class="muted space-top">正在读取执行记录…</p>
    <p v-else-if="!ordered.length" class="muted space-top">{{ ended ? '本轮没有执行记录，请结合审查单元和结束原因查看结果。' : '准备期间尚无执行记录，开始分析后会在此显示。' }}</p>
    <ol v-else class="rg-stage-list">
      <li v-for="item in ordered" :key="item.id">
        <StatusBadge :status="item.status" :label="workLabel[item.status]" />
        <span v-if="hasUnresolvedInvestigation(item)" class="notice warn small">关键检查待核实</span>
        <div class="min0">
          <strong>{{ item.kind === 'supplement' ? '补查缺口' : item.kind === 'review' ? '分析代码' : '核验疑似问题' }} · {{ workDescription(item) }}</strong>
          <p v-if="workPaths(item)" class="muted small">{{ workPaths(item) }}</p>
          <p class="rg-activity-meta"><span>{{ materialLabel[item.material_delivery] }}</span><span>尝试 {{ item.attempt }} 次</span><span>工具调用 {{ item.tool_calls }} 次</span></p>
          <p v-if="item.reused_from" class="notice small">复用了此前已完成且源码一致的审查单元。历史问题仍由本轮独立核验。</p>
          <p v-if="item.error_code" class="notice warn small">{{ executionErrorLabel(item.error_code) }}</p>
          <div v-if="item.kind !== 'verification' && item.result?.unresolved" class="notice warn small">
            <p>必要检查：{{ item.result.unresolved.required_check }}</p>
            <p>源码观察：{{ item.result.unresolved.observation }}</p>
            <p>缺失事实：{{ item.result.unresolved.missing_premise }}</p>
            <p>阻塞原因：{{ item.result.unresolved.why_required }}</p>
          </div>
          <p v-else-if="item.kind === 'review' && item.result?.unresolved_reason" class="notice warn small">{{ item.result.unresolved_reason }}</p>
          <details v-if="item.error_code || item.reused_from || Object.keys(item.missing_material_reasons).length" class="space-top">
            <summary class="link">查看执行详情</summary>
            <p v-if="item.error_code" class="muted small">错误类别：<code>{{ item.error_code }}</code></p>
            <template v-if="item.error_diagnostic">
              <p class="muted small">失败阶段：{{ executionStageLabel(item.error_diagnostic.stage) }} · {{ item.error_diagnostic.error_type }}</p>
              <p v-if="item.error_diagnostic.response_received" class="muted small">已收到模型响应；{{ item.error_diagnostic.usage_known ? '用量已保留' : '用量仍未知' }}，响应到达不代表核验成功。</p>
              <p v-if="item.error_diagnostic.code" class="muted small">诊断码：<code>{{ item.error_diagnostic.code }}</code></p>
              <p v-if="item.error_diagnostic.http_status" class="muted small">HTTP 状态：{{ item.error_diagnostic.http_status }}</p>
              <p v-if="item.error_diagnostic.ticket" class="muted small">请求票据：<code>{{ item.error_diagnostic.ticket }}</code></p>
              <p v-if="item.error_diagnostic.request_id" class="muted small">上游请求 ID：<code>{{ item.error_diagnostic.request_id }}</code></p>
            </template>
            <p v-if="item.reused_from" class="muted small">复用来源：<code>{{ item.reused_from }}</code></p>
            <pre v-if="Object.keys(item.missing_material_reasons).length" class="rg-json">{{ JSON.stringify(item.missing_material_reasons, null, 2) }}</pre>
          </details>
        </div>
      </li>
    </ol>
  </details>

  <details class="card card-pad progress-disclosure space-top">
    <summary>用量与执行详情 <span>模型请求、Token 与缓存</span></summary>
    <p class="muted small space-top">显示服务端已记录的用量。正在进行或缺少供应商明细的请求可能显示为未知，不代表零消耗。</p>
    <div class="rg-activity-stats rg-activity-stats-wide space-top">
      <div><small>模型调用</small><strong>{{ count(usage.calls) }}</strong><span>次请求</span></div>
      <div><small>总输入 Token</small><strong>{{ count(usage.input) }}</strong><span>包含命中与未命中缓存</span></div>
      <div><small>输入缓存</small><strong>{{ count(usage.cachedInput) }}</strong><span>{{ usage.cacheHitRate == null ? '命中率未知' : `命中率 ${(usage.cacheHitRate * 100).toFixed(1)}%` }}</span></div>
      <div><small>未缓存输入</small><strong>{{ count(usage.uncachedInput) }}</strong><span>供应商未命中缓存</span></div>
      <div><small>输出 Token</small><strong>{{ count(usage.output) }}</strong><span>模型生成量</span></div>
      <div><small>当前阶段</small><strong>{{ phaseLabel(task.phase) }}</strong><span>{{ task.configuration.model }}</span></div>
    </div>
    <p v-if="(props.usage ?? report?.usage)?.cache_unknown_calls" class="muted small space-top">部分请求缺少供应商缓存明细，缓存合计与命中率保持未知。</p>
    <p v-if="task.plan.grouping_status" class="muted small space-top">{{ groupingLabel[task.plan.grouping_status] }}。文件分组仅用于组织调查。</p>
  </details>
  <section v-if="report?.reasons.length" class="card card-pad space-top">
    <h3>需要注意的限制</h3>
    <p v-for="reason in report.reasons" :key="reason">{{ reviewReasonLabel(reason) }}</p>
  </section>
</template>
