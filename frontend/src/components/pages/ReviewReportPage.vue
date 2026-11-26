<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { getPublicationCapability } from '../../api/client';
import { useReviewCommentCopy } from '../../composables/useReviewCommentCopy';
import type { Run } from '../../types/review';
import type { PublicationPlan } from '../../types/publication';
import type { PublicationCapability } from '../../types/operations';
import { terminal } from '../../utils/nativeReview';
import AppIcon from '../common/AppIcon.vue';
import ReportMarkdown from '../report/ReportMarkdown.vue';
import GitHubPublicationPanel from '../review/publication/GitHubPublicationPanel.vue';
const props = defineProps<{ task: Run; plan: PublicationPlan | null; report: string; loading: boolean; publishOpen?: boolean }>(); const emit = defineEmits<{ export: []; 'update:publishOpen': [value: boolean] }>();
const showPublish = computed({ get: () => props.publishOpen ?? false, set: value => emit('update:publishOpen', value) }), capability = ref<PublicationCapability | null>(null), capabilityError = ref(false);
const shortHead = computed(() => props.task.snapshot.head_sha.slice(0,8));
// 页面工具栏已显示标题，正文去掉重复的一级标题；导出文件仍保留。
const displayReport = computed(() => props.report.replace(/^#\s+Rivu 审查(?:评论|报告)\s*(?:\r?\n)+/, ''));
const commentMarkdown = computed(() => {
  const plan = props.plan;
  if (!plan || plan.review_id !== props.task.id || plan.snapshot.head_sha !== props.task.snapshot.head_sha) return '';
  return plan.review_comment_markdown || (props.task.source ? plan.summary_markdown : '');
});
const { copied, feedback, checking, pendingCopy, previewMarkdown, writeClipboard, copyCheckedComment,
  copyAcknowledged, retryPending, cancelPending } = useReviewCommentCopy({
  reviewId: () => props.task.id,
  reviewedHeadSha: () => props.task.snapshot.head_sha,
  version: () => props.plan?.report_version,
  markdown: () => commentMarkdown.value,
  options: () => ({ detail: 'compact', exclude_resolved: false }),
});
const previewChanged = computed(() => previewMarkdown.value !== commentMarkdown.value);
async function copyComment() {
  if (!commentMarkdown.value) return;
  if (props.task.source) await writeClipboard(commentMarkdown.value);
  else await copyCheckedComment();
}
async function loadCapability() {
  capabilityError.value = false;
  try { capability.value = await getPublicationCapability(); }
  catch { capabilityError.value = true; }
}
watch(() => !!props.task.source, local => { if (!local) void loadCapability(); });
onMounted(() => { if (!props.task.source) void loadCapability(); });
</script>
<template>
<div class="rd-workspace">
<div class="rd-heading">
<div>
<h2>{{ task.source ? '审查报告' : '发布' }}</h2>
<p>基于提交 <code>{{ shortHead }}</code> · 已保存的审查结果</p>
</div>
<details class="rd-snapshot-info">
<summary>快照与交付边界 <AppIcon name="chevron-down" :size="14" /></summary>
<dl>
<div>
<dt>HEAD</dt>
<dd>
<code>{{ task.snapshot.head_sha }}</code>
</dd>
</div>
<div>
<dt>有效 BASE</dt>
<dd>
<code>{{ task.snapshot.base_sha }}</code>
</dd>
</div>
<div>
<dt>声明 BASE</dt>
<dd>
<code>{{ task.snapshot.declared_base_sha }}</code>
</dd>
</div>
</dl>
<p>未执行目标仓库代码、测试或构建。{{ task.source ? '本地快照提供审查报告，不支持 GitHub 发布。' : '发布资格由服务端独立校验。' }}</p>
</details>
</div>
<div class="rd-shell">
<div class="rd-toolbar">
<div class="rd-toolbar__title">
<span class="rd-toolbar__icon"><AppIcon name="report" :size="16" /></span>
<div>
<strong>审查评论</strong>
<small>Markdown 文档 · 固定 Head <code>{{ shortHead }}</code></small>
</div>
</div>
<div class="rd-toolbar__actions">
<button class="btn secondary" :disabled="!report" @click="$emit('export')">
<AppIcon name="download" />导出评论</button>
<button class="btn secondary" :disabled="!commentMarkdown || checking || !!pendingCopy" @click="copyComment">
<AppIcon name="copy" />{{ checking ? '正在检查…' : copied ? '已复制' : previewChanged ? '确认复制更新后的评论' : '复制评论' }}</button>
<button v-if="!task.source" class="btn secondary" :aria-expanded="showPublish" @click="showPublish = !showPublish">
<AppIcon name="send" />{{ showPublish ? '收起发布面板' : '发布到 GitHub' }}</button>
</div>
</div>
<div class="rd-document">
<p v-if="feedback" role="status">{{ feedback }}</p>
<div v-if="pendingCopy" class="row">
<button class="btn secondary" @click="copyAcknowledged">确认复制带快照说明的评论</button>
<button class="btn secondary" @click="retryPending">重试检查</button>
<button class="btn secondary" @click="cancelPending">取消复制</button>
</div>
<details v-if="!task.source && previewMarkdown" :open="previewChanged || !!pendingCopy || undefined">
<summary>复制评论预览</summary>
<ReportMarkdown :content="previewMarkdown" />
</details>
<p v-if="loading" class="muted">正在读取报告…</p>
<ReportMarkdown v-else-if="report" :content="displayReport" />
<p v-else class="muted">尚无可读取的 Markdown 报告。</p>
</div>
</div>
<GitHubPublicationPanel v-if="!task.source && showPublish && plan && terminal(task.status)" :review-id="task.id" :plan="plan" :capability="capability" :capability-error="capabilityError" @retry-capability="loadCapability" @result="() => {}" />
</div>
</template>
