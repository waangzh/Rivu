<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, toRaw, watch } from 'vue';
import { ApiError, cancelReview, createReview, getAvailableModels, getReview, getReviewCapabilities, retryOverview, retryReview, subscribeToReview } from './api/client';
import type { AppPage } from './types/operations';
import type { Run } from './types/review';
import type { ReviewCapabilities, ReviewPlanOptions } from './types/reviewPlan';
import { reviewPrimarySections, type ReviewSection } from './types/workspace';
import { defaultPlan, phaseLabel, terminal } from './utils/nativeReview';
import { initialReviewSection, reviewRecordMissing } from './utils/reviewProgress';
import { creationWasRejected, readPendingCreation, saveAcceptedReview, savePendingCreation, type PendingReviewCreation } from './utils/reviewCreation';
import { publicationErrorText } from './utils/publicationPresentation';
import AppSidebar from './components/layout/AppSidebar.vue';
import AppHeader from './components/layout/AppHeader.vue';
import NewReviewPage from './components/pages/NewReviewPage.vue';
import ReviewSubmissionPage from './components/pages/ReviewSubmissionPage.vue';
import ReviewHistoryPage from './components/pages/ReviewHistoryPage.vue';
import SettingsPage from './components/pages/SettingsPage.vue';
import ReviewWorkspace from './components/review/ReviewWorkspace.vue';

const page = ref<AppPage>('dashboard'), section = ref<ReviewSection>('overview'), mobileOpen = ref(false);
const prUrl = ref(''), model = ref(''), models = ref<string[]>([]);
const autoPublish = ref(false), publicationToken = ref('');
const capabilities = ref<ReviewCapabilities | null>(null);
const options = ref<ReviewPlanOptions>(defaultPlan());
const task = ref<Run | null>(null), trackedId = ref<string | null>(null);
const creation = ref<PendingReviewCreation | null>(null);
const creationError = ref(''), creationRejected = ref(false);
const creationRecoverable = ref(false);
const error = ref(''), connectionError = ref(''), busy = ref(false), opening = ref(false), restoring = ref(true);
let controller: AbortController | null = null;
let creationController: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let closeStream: (() => void) | null = null;
let generation = 0, catalogGeneration = 0;
let disposed = false;

const taskActive = computed(() => !!creation.value || (!!task.value && !terminal(task.value.status)));
const pageTitle = computed(() => page.value === 'history' ? '审查历史' : page.value === 'settings' ? '系统设置'
  : creation.value ? '审查进度' : task.value ? reviewPrimarySections.find(item => item.id === section.value)?.label || '当前审查'
    : trackedId.value ? '审查进度' : '新建审查');
const statusText = computed(() => creation.value ? (busy.value ? '正在创建审查' : '等待处理创建结果')
  : task.value ? phaseLabel(task.value.phase) : '只读静态审查');

function forgetTask() {
  generation++;
  controller?.abort();
  creationController?.abort();
  closeStream?.();
  closeStream = null;
  if (timer) clearTimeout(timer);
  timer = null;
  task.value = null;
  trackedId.value = null;
  opening.value = false;
  busy.value = false;
  restoring.value = false;
  connectionError.value = '';
  autoPublish.value = false;
  publicationToken.value = '';
  try { localStorage.removeItem('rg-review-task'); } catch {}
}
function newReview() {
  if (creation.value) return;
  forgetTask();
  error.value = '';
  page.value = 'dashboard';
  section.value = 'overview';
  mobileOpen.value = false;
  prUrl.value = '';
}
function navigate(value: ReviewSection) {
  page.value = 'dashboard';
  section.value = value;
  mobileOpen.value = false;
}
function rereview() {
  if (!task.value || !terminal(task.value.status)) return;
  const url = task.value.pr_url, saved = task.value.configuration.options;
  newReview();
  prUrl.value = url;
  options.value = structuredClone(toRaw(saved));
}
async function loadModels() {
  const owner = ++catalogGeneration;
  try {
    const value = await getAvailableModels();
    if (!disposed && owner === catalogGeneration) models.value = value.models.map(item => item.id);
  } catch {}
}

async function follow(id: string, initial?: Run, activate = true, selectDefault = false) {
  if (disposed || (creation.value && !initial)) return;
  if (activate) page.value = 'dashboard';
  mobileOpen.value = false;
  const changed = trackedId.value !== id;
  const chooseDefault = changed || !task.value || selectDefault;
  if (changed) task.value = null;
  trackedId.value = id;
  opening.value = !initial;
  busy.value = false;
  closeStream?.();
  closeStream = null;
  controller?.abort();
  if (timer) clearTimeout(timer);
  const owner = ++generation, abort = new AbortController();
  controller = abort;
  let refreshing = false, chooseSection = chooseDefault;
  try { localStorage.setItem('rg-review-task', id); } catch {}

  function accept(result: Run) {
    if (result.id !== id) throw new Error('返回的记录不属于当前审查任务');
    task.value = result;
    if (chooseSection) {
      section.value = initialReviewSection(result.status);
      chooseSection = false;
    }
    connectionError.value = '';
    error.value = '';
  }
  async function refresh() {
    if (owner !== generation || refreshing) return;
    refreshing = true;
    if (timer) clearTimeout(timer);
    try {
      const result = await getReview(id, abort.signal);
      if (owner !== generation) return;
      accept(result);
      if (!terminal(result.status)) timer = setTimeout(refresh, 1500);
      else { closeStream?.(); closeStream = null; }
    } catch (cause) {
      if (owner !== generation || abort.signal.aborted) return;
      if (reviewRecordMissing(cause)) {
        forgetTask();
        error.value = '该审查记录不存在或已被删除，已停止重试。';
        return;
      }
      connectionError.value = '连接暂时中断，正在重新获取状态。后台任务是否仍在运行尚未确认。';
      if (!task.value) error.value = '暂时无法打开审查记录，系统会继续重试。';
      timer = setTimeout(refresh, 3000);
    } finally {
      refreshing = false;
      if (owner === generation) opening.value = false;
    }
  }
  if (initial) accept(initial);
  else await refresh();
  if (owner === generation && task.value && !terminal(task.value.status)) {
    closeStream = subscribeToReview(id, () => { void refresh(); });
    if (initial) void refresh();
  }
}

async function openReview(id: string) {
  await follow(id, undefined, true, true);
}

async function submitCreation() {
  if (!creation.value || busy.value) return;
  if (creation.value.input.auto_publish && !publicationToken.value) return;
  const pending = toRaw(creation.value), owner = generation;
  const abort = new AbortController();
  creationController = abort;
  busy.value = true;
  creationError.value = '';
  creationRejected.value = false;
  try {
    const result = await createReview(pending.input, abort.signal, pending.key, publicationToken.value);
    if (disposed || owner !== generation) return;
    saveAcceptedReview(result.id);
    creation.value = null;
    publicationToken.value = '';
    await follow(result.id, result, page.value === 'dashboard');
  } catch (cause) {
    if (disposed || owner !== generation || abort.signal.aborted) return;
    creationRejected.value = creationWasRejected(cause);
    creationError.value = cause instanceof ApiError
      ? (pending.input.auto_publish ? publicationErrorText(cause.message, cause.message) : cause.message)
      : '未收到确定的创建结果。继续确认会沿用原请求标识，避免重复创建审查。';
    if (creationRejected.value) savePendingCreation(null);
  } finally {
    if (!disposed && owner === generation) busy.value = false;
  }
}
function start() {
  if (busy.value || creation.value) return;
  if (autoPublish.value && !publicationToken.value) { error.value = '请先输入发布操作口令。'; return; }
  const input = structuredClone(toRaw(options.value));
  input.paths = input.paths?.map(path => path.trim()).filter(Boolean) || null;
  if (!input.focus.length) { error.value = '请至少选择一个审查重点。'; return; }
  error.value = '';
  page.value = 'dashboard';
  section.value = 'activity';
  mobileOpen.value = false;
  creation.value = {
    key: crypto.randomUUID(), startedAt: new Date().toISOString(),
    input: { pr_url: prUrl.value, ...input, ...(model.value ? { model: model.value } : {}),
      ...(autoPublish.value ? { auto_publish: true } : {}) },
  };
  creationRecoverable.value = savePendingCreation(toRaw(creation.value));
  void submitCreation();
}
function editCreation() {
  if (!creationRejected.value || busy.value) return;
  creation.value = null;
  creationError.value = '';
  creationRejected.value = false;
  savePendingCreation(null);
}
async function stop() {
  if (!task.value || busy.value) return;
  busy.value = true;
  try { await cancelReview(task.value.id); await follow(task.value.id); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '停止失败'; }
  finally { if (!disposed) busy.value = false; }
}
async function retry() {
  if (!task.value || busy.value) return;
  busy.value = true;
  try {
    const result = await retryReview(task.value.id);
    section.value = 'activity';
    await follow(result.id, result);
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '重试失败'; }
  finally { if (!disposed) busy.value = false; }
}
async function retrySummary() {
  if (!task.value || busy.value) return;
  busy.value = true;
  try { const result = await retryOverview(task.value.id); await follow(result.id, result); }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '重试总览失败'; }
  finally { if (!disposed) busy.value = false; }
}

watch(page, () => { mobileOpen.value = false; });
watch(pageTitle, title => { if (typeof document !== 'undefined') document.title = title + ' · Rivu'; });
watch([page, () => task.value?.id, () => !!creation.value], async () => {
  await nextTick();
  if (!disposed && page.value === 'dashboard') document.querySelector<HTMLElement>('#workspace-content h1')?.focus();
});
onMounted(async () => {
  void loadModels();
  void getReviewCapabilities().then(value => {
    if (disposed) return;
    capabilities.value = value;
    if (!creation.value && !task.value && !prUrl.value) options.value.include_pr_discussions = value.pr_discussions_default ?? false;
  }).catch(() => { if (!disposed) error.value = '审查选项读取失败，创建时仍将由服务端校验。'; });
  try {
    const pending = readPendingCreation();
    if (pending) {
      creation.value = pending;
      creationRecoverable.value = true;
      prUrl.value = pending.input.pr_url;
      model.value = pending.input.model || '';
      autoPublish.value = pending.input.auto_publish ?? false;
      options.value = {
        depth: pending.input.depth, paths: pending.input.paths, focus: pending.input.focus,
        include_pr_discussions: pending.input.include_pr_discussions ?? false,
        limits: { ...defaultPlan().limits, ...pending.input.limits },
      };
      section.value = 'activity';
      await submitCreation();
    } else {
      let saved: string | null = null;
      try { saved = localStorage.getItem('rg-review-task'); } catch {}
      if (saved) await follow(saved);
    }
  } finally { if (!disposed) restoring.value = false; }
});
onBeforeUnmount(() => {
  disposed = true;
  generation++;
  catalogGeneration++;
  controller?.abort();
  creationController?.abort();
  closeStream?.();
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <div class="app rg-app" @keydown.esc="mobileOpen = false">
    <button v-if="mobileOpen" class="rg-nav-backdrop" aria-label="关闭导航遮罩" @click="mobileOpen = false" />
    <AppSidebar v-model="page" :section="section" :has-review="!!task" :has-pending="!!creation || !!trackedId || restoring" :task-active="taskActive" :mobile-open="mobileOpen" :busy="busy || opening" @section="navigate" @create="newReview" @close="mobileOpen = false" />
    <div class="main">
      <AppHeader :title="pageTitle" :status-text="statusText" :menu-open="mobileOpen" @menu="mobileOpen = !mobileOpen" />
      <main id="workspace-content" class="page">
        <p v-if="error" class="notice warn space-bottom" role="alert">{{ error }}</p>
        <ReviewHistoryPage v-if="page === 'history'" :busy="busy || opening || !!creation" @open="openReview" @create="newReview" @deleted="task?.id === $event && forgetTask()" />
        <SettingsPage v-else-if="page === 'settings'" @saved="loadModels" />
        <template v-else>
          <ReviewSubmissionPage v-if="creation" v-model:publication-token="publicationToken" :creation="creation" :submitting="busy" :rejected="creationRejected" :error="creationError" :recoverable="creationRecoverable" @retry="submitCreation" @edit="editCreation" @history="page = 'history'" />
          <ReviewWorkspace v-else-if="task" :key="task.id" :task="task" :section="section" :busy="busy || opening" :connection-error="connectionError" @navigate="navigate" @cancel="stop" @retry="retry" @overview-retry="retrySummary" @rereview="rereview" />
          <section v-else-if="restoring || opening || trackedId" class="card card-pad workspace-loading" role="status">
            <h1 tabindex="-1">正在打开审查记录…</h1>
            <p class="muted space-top">正在恢复任务状态，请稍候。</p>
            <button v-if="trackedId && !opening" class="btn secondary space-top" @click="follow(trackedId)">重试打开任务</button>
          </section>
          <NewReviewPage v-else v-model:pr-url="prUrl" v-model:model="model" v-model:options="options" v-model:auto-publish="autoPublish" v-model:publication-token="publicationToken" :models="models" :capabilities="capabilities" :busy="busy" @create="start" @open="openReview" @history="page = 'history'" />
        </template>
      </main>
    </div>
  </div>
</template>
