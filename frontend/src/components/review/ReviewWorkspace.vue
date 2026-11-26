<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { getFindings, getProgress, getPublicationPlan, getReport } from '../../api/client';
import type { Candidate, Finding, FindingGroup, Report, ReviewProgress, Run } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import { reviewPrimarySections, type ReviewSection } from '../../types/workspace';
import { executionErrorLabel, phaseLabel, reviewTitle, safeGitHubLink, terminal } from '../../utils/nativeReview';
import { initialReviewSection } from '../../utils/reviewProgress';
import AppIcon from '../common/AppIcon.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewActivityPage from '../pages/ReviewActivityPage.vue';
import ReviewFilesPage from '../pages/ReviewFilesPage.vue';
import ReviewFindingsPage from '../pages/ReviewFindingsPage.vue';
import ReviewOverviewPage from '../pages/ReviewOverviewPage.vue';
import ReviewReportPage from '../pages/ReviewReportPage.vue';
import AutomaticPublicationStatus from './publication/AutomaticPublicationStatus.vue';

const props = defineProps<{ task: Run; section?: ReviewSection; busy?: boolean; connectionError?: string }>();
const emit = defineEmits<{ navigate: [value: ReviewSection]; cancel: []; retry: []; overviewRetry: []; rereview: []; refresh: [] }>();

const localSection = ref<ReviewSection>(initialReviewSection(props.task.status));
const tab = computed({
  get: () => props.section ?? localSection.value,
  set: value => {
    localSection.value = value;
    emit('navigate', value);
  },
});
const publishOpen = ref(false);
async function inspectPublication() {
  tab.value = 'publish';
  publishOpen.value = true;
  // 计划与发布面板可能仍在加载，等交付记录出现后再定位；能力读取失败时退回面板本身。
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await nextTick();
    const record = document.querySelector<HTMLDetailsElement>('.gp-record');
    if (record) {
      record.open = true;
      record.scrollIntoView({ block: 'start', behavior: 'instant' });
      return;
    }
    const panel = document.querySelector('.github-publication');
    if (panel && attempt >= 10) {
      panel.scrollIntoView({ block: 'start', behavior: 'instant' });
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
const plan = ref<PublicationPlan | null>(null);
const findings = ref<Finding[]>([]);
const candidates = ref<Candidate[]>([]);
const findingGroups = ref<FindingGroup[]>([]);
const findingsLoaded = ref(false);
const progress = ref<ReviewProgress | null>(null);
const syncedAt = ref<number | null>(null);
const progressError = ref('');
const report = ref<Report | null>(null);
const markdown = ref('');
const error = ref('');
const loading = ref(false);
const planError = ref('');
const resultKey = computed(() => JSON.stringify([
  props.task.id, props.task.snapshot.head_sha, props.task.report?.version ?? null,
  terminal(props.task.status) ? props.task.status : 'active',
]));
let resultRequest: AbortController | null = null;
let progressRequest: AbortController | null = null;
let resultQueued = false;
let progressQueued = false;
let reportLoaded = false;
let reportVersion: string | null = null;

async function loadProgress() {
  if (progressRequest) { progressQueued = true; return; }
  const request = new AbortController();
  progressRequest = request;
  try {
    const result = await getProgress(props.task.id, request.signal);
    if (request.signal.aborted) return;
    progress.value = result;
    syncedAt.value = Date.now();
    progressError.value = '';
  } catch {
    if (!request.signal.aborted) {
      progressError.value = '进度读取失败，请重试。';
    }
  } finally {
    if (progressRequest === request) {
      progressRequest = null;
      if (progressQueued) { progressQueued = false; void loadProgress(); }
    }
  }
}

async function loadResults() {
  if (resultRequest) { resultQueued = true; return; }
  const { id, status, snapshot } = props.task;
  const version = props.task.report?.version ?? null;
  const finished = terminal(status);
  const readFindings = !finished || !findingsLoaded.value;
  const readReport = !reportLoaded && (version !== null || finished);
  const readPlan = finished && !plan.value;
  if (!readFindings && !readReport && !readPlan) return;

  const request = new AbortController();
  resultRequest = request;
  loading.value = !findingsLoaded.value || readReport;
  try {
    const [nextFindings, nextReport, nextPlan] = await Promise.allSettled([
      readFindings ? getFindings(id, request.signal) : null,
      readReport ? getReport(id, request.signal) : null,
      readPlan ? getPublicationPlan(id, request.signal) : null,
    ]);
    if (request.signal.aborted) return;

    let resultFailed = false;
    if (readFindings) {
      if (nextFindings.status === 'fulfilled' && nextFindings.value?.report_version === version) {
        const values = nextFindings.value;
        findings.value = values.findings;
        candidates.value = values.candidates;
        findingGroups.value = values.finding_groups;
        findingsLoaded.value = true;
      } else resultFailed = true;
    }
    if (readReport) {
      if (nextReport.status === 'fulfilled' && nextReport.value && (nextReport.value.report?.version ?? null) === version) {
        report.value = nextReport.value.report;
        markdown.value = nextReport.value.markdown;
        reportVersion = nextReport.value.report_version;
        reportLoaded = true;
      } else resultFailed = true;
    }
    error.value = resultFailed ? '暂时无法读取与当前审查版本一致的结果。' : '';
    if (readPlan) {
      const value = nextPlan.status === 'fulfilled' ? nextPlan.value : null;
      if (value && value.review_id === id && value.snapshot.head_sha === snapshot.head_sha
          && value.digest.execution_status === status && value.report_version === (version ?? reportVersion)) {
        plan.value = value;
        planError.value = '';
      } else {
        planError.value = '暂时无法读取与当前审查版本一致的发布计划。';
      }
    }
  } finally {
    if (resultRequest === request) {
      resultRequest = null;
      loading.value = false;
      if (resultQueued) { resultQueued = false; void loadResults(); }
    }
  }
}

function refreshData() {
  emit('refresh');
  void loadProgress();
  void loadResults();
}

function abortReads() {
  resultRequest?.abort();
  progressRequest?.abort();
  resultRequest = progressRequest = null;
  resultQueued = progressQueued = false;
}
watch(resultKey, () => {
  abortReads();
  plan.value = null;
  report.value = null;
  markdown.value = '';
  findings.value = [];
  candidates.value = [];
  findingGroups.value = [];
  findingsLoaded.value = reportLoaded = false;
  reportVersion = null;
  progress.value = null;
  syncedAt.value = null;
  error.value = planError.value = progressError.value = '';
}, { flush: 'sync' });

// Work-item events can change progress and candidates without changing run.updated_at.
watch(() => props.task, () => {
  if (!terminal(props.task.status) || !progress.value) void loadProgress();
  void loadResults();
}, { immediate: true });
onBeforeUnmount(abortReads);

function exportReport() {
  if (!markdown.value) return;

  const url = URL.createObjectURL(new Blob([markdown.value], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'Rivu-' + props.task.id + '-' + props.task.snapshot.head_sha.slice(0, 8) + '.md';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
</script>

<template>
  <header class="pr-title">
    <div class="min0">
      <div class="eyebrow">{{ task.snapshot.repository }}{{ task.pr ? ' / PR #' + task.pr.number : ' / 本地固定快照' }}</div>
      <h1 tabindex="-1">{{ reviewTitle(task) }}</h1>
      <div class="meta">
        <StatusBadge :status="task.status" />
        <span class="muted small">{{ phaseLabel(task.phase) }}</span>
        <code>{{ task.snapshot.base_sha.slice(0, 8) }} → {{ task.snapshot.head_sha.slice(0, 8) }}</code>
      </div>
    </div>
    <div class="head-actions">
      <a v-if="safeGitHubLink(task.pr?.html_url)" class="btn secondary" :href="safeGitHubLink(task.pr?.html_url)" target="_blank" rel="noreferrer">
        GitHub <AppIcon name="external" :size="14" />
      </a>
      <button v-if="!terminal(task.status)" class="btn secondary" :disabled="busy" @click="$emit('cancel')">停止审查</button>
      <button v-if="task.status === 'failed' || task.status === 'partial'" class="btn secondary" :disabled="busy" @click="$emit('retry')">重试审查</button>
      <button v-if="terminal(task.status)" class="btn secondary" :disabled="busy" @click="$emit('rereview')">{{ task.source ? '重新审查此快照' : '重新审查此 PR' }}</button>
    </div>
  </header>

  <AutomaticPublicationStatus v-if="!task.source" :review-id="task.id" @inspect="inspectPublication" />

  <nav class="subnav" aria-label="审查结果导航">
    <button v-for="item in reviewPrimarySections" :key="item.id" :class="{ active: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id">
      {{ task.source && item.id === 'publish' ? '报告' : item.label }}{{ item.id === 'findings' && findings.length ? ' (' + findings.length + ')' : '' }}
    </button>
  </nav>

  <p v-if="error" class="notice warn space-bottom" role="alert">
    {{ error }} <button class="link" @click="refreshData">重试读取结果</button>
  </p>
  <p v-if="tab === 'publish' && planError" class="notice warn space-bottom" role="alert">
    {{ planError }} <button class="link" @click="refreshData">重试读取发布计划</button>
  </p>
  <p v-if="connectionError" class="notice warn space-bottom" role="alert">{{ connectionError }}</p>
  <p v-if="task.error_code" class="notice warn space-bottom">
    {{ executionErrorLabel(task.error_code) }}。错误类别：<code>{{ task.error_code }}</code>；缺失与未知不能视为通过。
  </p>

  <ReviewOverviewPage v-if="tab === 'overview'" :task="task" :report="report" :findings="findings" :loading="loading" @navigate="tab = $event" @retry-overview="$emit('overviewRetry')" />
  <ReviewFilesPage v-else-if="tab === 'changes'" :task="task" :findings="findings" @finding="tab = 'findings'" />
  <ReviewFindingsPage v-else-if="tab === 'findings'" :task="task" :findings="findings" :candidates="candidates" :finding-groups="findingGroups" :deduplication="report?.finding_deduplication" :loading="loading" />
  <ReviewActivityPage v-else-if="tab === 'activity'" :task="task" :units="progress?.units ?? []" :usage="progress?.usage" :report="report" :candidates="candidates" :findings-count="findings.length" :findings-loaded="findingsLoaded" :loaded="!!progress" :synced-at="syncedAt" :sync-error="connectionError || progressError" @navigate="tab = $event" @refresh="refreshData" />
  <ReviewReportPage v-else :task="task" :plan="plan" :report="markdown" :loading="loading" :publish-open="publishOpen" @update:publish-open="publishOpen = $event" @export="exportReport" />

  <footer v-if="tab !== 'publish'" class="app-footer">
    <span>只读静态审查 · 未执行目标代码、测试或构建 · 结论仅适用于本次快照</span>
    <code>{{ task.snapshot.head_sha.slice(0, 12) }}</code>
  </footer>
</template>

<style scoped>
.pr-title h1 { overflow-wrap: anywhere; }
</style>
