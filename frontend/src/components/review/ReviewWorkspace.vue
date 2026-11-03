<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { getFindings, getProgress, getPublicationPlan, getReport } from '../../api/client';
import type { Candidate, Finding, Report, ReviewProgress, Run } from '../../types/review';
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
const emit = defineEmits<{ navigate: [value: ReviewSection]; cancel: []; retry: []; overviewRetry: []; rereview: [] }>();

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
const findingsLoaded = ref(false);
const progress = ref<ReviewProgress | null>(null);
const syncedAt = ref<number | null>(null);
const progressError = ref('');
const report = ref<Report | null>(null);
const markdown = ref('');
const error = ref('');
const loading = ref(false);
let controller: AbortController | null = null;
let progressController: AbortController | null = null;
let generation = 0;
let progressGeneration = 0;
let reloadQueued = false;
let progressLoading = false, progressQueued = false;

async function loadProgress() {
  if (progressLoading) { progressQueued = true; return; }
  const owner = ++progressGeneration;
  const request = new AbortController();
  progressController = request;
  progressLoading = true;
  try {
    const result = await getProgress(props.task.id, request.signal);
    if (owner !== progressGeneration) return;
    progress.value = result;
    syncedAt.value = Date.now();
    progressError.value = '';
  } catch {
    if (owner === progressGeneration && !request.signal.aborted) {
      progressError.value = '进度读取失败，当前显示的是上次取得的状态。';
    }
  } finally {
    progressLoading = false;
    if (owner === progressGeneration && progressQueued) {
      progressQueued = false;
      void loadProgress();
    }
  }
}

function refreshData() {
  void loadProgress();
  void load();
}

async function load() {
  if (loading.value) { reloadQueued = true; return; }
  const id = props.task.id;
  const owner = ++generation;
  controller?.abort();
  const request = new AbortController();
  controller = request;
  loading.value = true;
  error.value = '';

  try {
    const results = await Promise.allSettled([
      getFindings(id, request.signal),
      props.task.report || terminal(props.task.status)
        ? getReport(id, request.signal) : Promise.resolve({ report: null, markdown: '' }),
      terminal(props.task.status) ? getPublicationPlan(id, request.signal) : Promise.resolve(null),
    ]);
    if (owner !== generation) return;

    const [nextFindings, nextReport, nextPlan] = results;
    if (nextFindings.status === 'fulfilled') {
      findings.value = nextFindings.value.findings;
      candidates.value = nextFindings.value.candidates;
      findingsLoaded.value = true;
    }
    if (nextReport.status === 'fulfilled') {
      report.value = nextReport.value.report;
      markdown.value = nextReport.value.markdown;
    }
    if (nextPlan.status === 'fulfilled') plan.value = nextPlan.value;
    if (nextFindings.status === 'rejected' || nextReport.status === 'rejected') {
      error.value = '部分结果暂时无法读取，已有内容会保留。';
    }
  } catch (cause) {
    if (owner === generation && !request.signal.aborted) {
      error.value = cause instanceof Error ? cause.message : '结果读取失败';
    }
  } finally {
    if (owner === generation) {
      loading.value = false;
      if (reloadQueued) { reloadQueued = false; void load(); }
    }
  }
}

// Work-item events can change progress without changing the run's updated_at.
watch(() => props.task, refreshData, { immediate: true });
onBeforeUnmount(() => {
  generation += 1;
  progressGeneration += 1;
  controller?.abort();
  progressController?.abort();
});

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
  <p v-if="connectionError" class="notice warn space-bottom" role="alert">{{ connectionError }}</p>
  <p v-if="task.error_code" class="notice warn space-bottom">
    {{ executionErrorLabel(task.error_code) }}。错误类别：<code>{{ task.error_code }}</code>；缺失与未知不能视为通过。
  </p>

  <ReviewOverviewPage v-if="tab === 'overview'" :task="task" :report="report" :findings="findings" :loading="loading" @navigate="tab = $event" @retry-overview="$emit('overviewRetry')" />
  <ReviewFilesPage v-else-if="tab === 'changes'" :task="task" :findings="findings" @finding="tab = 'findings'" />
  <ReviewFindingsPage v-else-if="tab === 'findings'" :task="task" :findings="findings" :candidates="candidates" :loading="loading" />
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
