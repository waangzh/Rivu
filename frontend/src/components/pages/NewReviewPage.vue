<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, toRaw } from 'vue';
import { listReviews, previewReview } from '../../api/client';
import type { LocalGitRequest, ReviewPreviewResponse, Run } from '../../types/review';
import { reviewTitle } from '../../utils/nativeReview';
import type { ReviewCapabilities, ReviewPlanOptions } from '../../types/reviewPlan';
import AppIcon from '../common/AppIcon.vue';
import AppSelect from '../common/AppSelect.vue';
import StatusBadge from '../common/StatusBadge.vue';
import ReviewPlanControls from '../review/ReviewPlanControls.vue';
import AutomaticPublicationOptions from '../review/publication/AutomaticPublicationOptions.vue';
const props = defineProps<{ prUrl: string; localSource?: LocalGitRequest | null; model: string; models: string[]; options: ReviewPlanOptions; capabilities: ReviewCapabilities | null; busy: boolean; autoPublish?: boolean; publicationToken?: string }>();
const emit = defineEmits<{ 'update:prUrl': [value: string]; 'update:localSource': [value: LocalGitRequest | null]; 'update:model': [value: string]; 'update:options': [value: ReviewPlanOptions]; 'update:autoPublish': [value: boolean]; 'update:publicationToken': [value: string]; create: []; open: [id: string]; history: [] }>();
const repositories = computed(() => props.capabilities?.local_repositories || []);
const valid = computed(() => props.localSource
  ? !!props.localSource.repository_key && !!props.localSource.base_ref.trim() && !!props.localSource.head_ref.trim()
  : /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/[1-9][0-9]*\/?$/.test(props.prUrl));
const planCapabilities = computed(() => props.localSource && props.capabilities
  ? { ...props.capabilities, pr_discussions_available: false, pr_discussions_default: false } : props.capabilities);
const preview = ref<ReviewPreviewResponse | null>(null), previewError = ref(''), previewBusy = ref(false);
function selectSource(local: boolean) {
  preview.value = null;
  previewError.value = '';
  emit('update:localSource', local ? { kind: 'local_git', repository_key: repositories.value[0]?.key || '', base_ref: 'main', head_ref: 'HEAD' } : null);
  if (local) {
    emit('update:autoPublish', false);
    emit('update:publicationToken', '');
    emit('update:options', { ...structuredClone(toRaw(props.options)), include_pr_discussions: false });
  }
}
function editSource(field: 'repository_key' | 'base_ref' | 'head_ref', value: string) {
  if (!props.localSource) return;
  const source = { ...props.localSource, [field]: value };
  delete source.input_id;
  preview.value = null;
  previewError.value = '';
  emit('update:localSource', source);
}
async function freezePreview() {
  if (!props.localSource || !valid.value || previewBusy.value) return;
  previewBusy.value = true;
  previewError.value = '';
  try {
    const result = await previewReview({ source: props.localSource, ...props.options,
      include_pr_discussions: false, ...(props.model ? { model: props.model } : {}) }, controller.signal);
    if (controller.signal.aborted) return;
    preview.value = result;
    if (result.source) emit('update:localSource', { ...props.localSource, input_id: result.source.input_id });
  } catch (cause) {
    if (!controller.signal.aborted) previewError.value = cause instanceof Error ? cause.message : '快照准备失败，请检查提交版本。';
  } finally { previewBusy.value = false; }
}
const modelOptions = computed(() => [
  { value: '', label: '使用服务端默认模型' },
  ...(props.model && !props.models.includes(props.model) ? [{ value: props.model, label: props.model }] : []),
  ...props.models.map(id => ({ value: id, label: id })),
]);
const recent = ref<Run[]>([]), error = ref(''), loading = ref(true);
const controller = new AbortController();
onBeforeUnmount(() => controller.abort()); onMounted(async () => { try { const result = await listReviews('', 1, controller.signal);
if (!controller.signal.aborted) recent.value = result.items.slice(0, 3); } catch { if (!controller.signal.aborted) error.value = '最近记录加载失败，可在审查历史中重试。'; } finally { if (!controller.signal.aborted) loading.value = false; } });
</script>
<template>
<section class="hero-import">
<h1>{{ localSource ? '选择本地 Git 提交，' : '粘贴 GitHub PR 链接，' }}<br />直接发起只读代码审查。</h1>
<p>提交后会自动显示审查进度。系统先固定源码版本，再分析代码并独立核验疑似问题。</p>
<div class="source-switch" role="group" aria-label="审查来源">
<button class="btn" :class="localSource ? 'secondary' : 'primary'" :aria-pressed="!localSource" :disabled="busy || previewBusy" @click="selectSource(false)">GitHub PR</button>
<button class="btn" :class="localSource ? 'primary' : 'secondary'" :aria-pressed="!!localSource" :disabled="busy || previewBusy" @click="selectSource(true)">本地 Git</button>
</div>
<form @submit.prevent="valid && options.focus.length && !busy && $emit('create')">
<template v-if="!localSource">
<label class="field-label" for="pr-url">GitHub Pull Request 链接</label>
<div class="hero-input">
<input id="pr-url" class="input" type="url" required :value="prUrl" placeholder="https://github.com/owner/repo/pull/123" :disabled="busy" @input="$emit('update:prUrl', ($event.target as HTMLInputElement).value)" />
<button class="btn primary" :disabled="!valid || !options.focus.length || busy">
<AppIcon name="arrow-right" />{{ busy ? '正在提交…' : '开始审查' }}</button>
</div>
<p v-if="prUrl && !valid" class="input-error">请输入有效的 GitHub Pull Request 链接。</p>
</template>
<template v-else>
<p v-if="!repositories.length" class="notice warn">服务端尚未注册本地仓库。请配置 RIVU_LOCAL_REPOSITORIES 后重启服务。</p>
<div class="local-fields">
<label class="field-label">本地仓库
<AppSelect :model-value="localSource.repository_key" :options="repositories.map(item => ({ value: item.key, label: item.label }))" :disabled="busy || previewBusy" aria-label="本地仓库" @update:model-value="editSource('repository_key', $event)" />
</label>
<label class="field-label" for="local-base">Base 分支或完整 SHA
<input id="local-base" class="input" required :value="localSource.base_ref" :disabled="busy || previewBusy" @input="editSource('base_ref', ($event.target as HTMLInputElement).value)" />
</label>
<label class="field-label" for="local-head">Head 分支或完整 SHA
<input id="local-head" class="input" required :value="localSource.head_ref" :disabled="busy || previewBusy" @input="editSource('head_ref', ($event.target as HTMLInputElement).value)" />
</label>
</div>
<p class="muted small">读取服务器上的已注册仓库，仅审查已提交内容；暂存、未暂存和未跟踪文件不会纳入。以唯一 merge base 比较，不执行目标代码。</p>
<p v-if="previewError" class="input-error" role="alert">{{ previewError }}</p>
<p v-if="preview" class="notice" role="status">快照已固定：<code>{{ preview.snapshot.base_sha.slice(0, 8) }} → {{ preview.snapshot.head_sha.slice(0, 8) }}</code> · {{ preview.changed_files.length }} 个变更文件 · {{ preview.plan.units.length }} 个规划单元。启动将使用此快照。</p>
<div class="source-switch">
<button type="button" class="btn secondary" :disabled="!valid || busy || previewBusy" @click="freezePreview">{{ previewBusy ? '正在准备…' : '固定并预览快照' }}</button>
<button class="btn primary" :disabled="!valid || !options.focus.length || busy || previewBusy"><AppIcon name="arrow-right" />{{ busy ? '正在提交…' : '开始审查' }}</button>
</div>
</template>
</form>
<div class="hero-meta">
<span>
<AppIcon name="check-circle" />固定快照与可信证据</span>
<span>
<AppIcon name="shield" />严格只读代码审查</span>
</div>
</section>
<div class="grid twoone space-top">
<section class="card card-pad">
<div class="card-header">
<div>
<h2>审查偏好</h2>
<p>可按需要调整审查重点、范围和用量上限。</p>
</div>
</div>
<details>
<summary class="link">调整深度、重点与范围 · {{ {quick:'快速初筛',standard:'标准审查',deep:'深度核验'}[options.depth] }}</summary>
<ReviewPlanControls :model-value="options" :capabilities="planCapabilities" :disabled="busy" @update:model-value="$emit('update:options', $event)" />
</details>
<div class="field-label space-top model-field">
<span>执行模型</span>
<AppSelect :model-value="model" :options="modelOptions" :disabled="busy" aria-label="执行模型"
  @update:model-value="$emit('update:model', $event)" />
</div>
<AutomaticPublicationOptions v-if="!localSource" :model-value="autoPublish ?? false" :token="publicationToken ?? ''" :disabled="busy"
  @update:model-value="$emit('update:autoPublish', $event)" @update:token="$emit('update:publicationToken', $event)" />
</section>
<section class="card card-pad">
<div class="card-header">
<h2>最近审查</h2>
<button class="link" @click="$emit('history')">全部记录 →</button>
</div>
<p v-if="loading || error" class="muted small" role="status">{{ error || '正在读取最近记录…' }}</p>
<p v-else-if="!recent.length" class="muted small">还没有审查记录。从一个 Pull Request 开始。</p>
<button v-for="item in recent" :key="item.id" class="recent-row" :disabled="busy" @click="$emit('open', item.id)">
<AppIcon name="branch" />
<span class="recent-identity">
<strong>{{ item.pr ? '#' + item.pr.number + ' ' : '' }}{{ reviewTitle(item) }}</strong>
<small>{{ item.snapshot.repository }}</small>
</span>
<StatusBadge :status="item.status" />
</button>
</section>
</div>
</template>

<style scoped>
.model-field { max-width: 380px; }
.source-switch { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
.local-fields { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 14px; }
.local-fields .input { width: 100%; margin-top: 8px; }
@media (max-width: 760px) { .local-fields { grid-template-columns: 1fr; } }
</style>
