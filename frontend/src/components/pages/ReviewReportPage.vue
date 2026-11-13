<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'; import { getPublicationCapability } from '../../api/client'; import type { ReviewReportResponse, Run } from '../../types/review'; import type { PublicationPlan } from '../../types/publication'; import type { PublicationCapability } from '../../types/operations'; import { terminal } from '../../utils/nativeReview'; import AppIcon from '../common/AppIcon.vue'; import ReportMarkdown from '../report/ReportMarkdown.vue'; import ReportSummaryBar from '../report/ReportSummaryBar.vue'; import GitHubPublicationPanel from '../review/publication/GitHubPublicationPanel.vue';
const props = defineProps<{ task: Run; plan: PublicationPlan | null; report: string; summary?: ReviewReportResponse | null; loading: boolean; publishOpen?: boolean }>(); const emit = defineEmits<{ export: []; 'update:publishOpen': [value: boolean] }>();
const showPublish = computed({ get: () => props.publishOpen ?? false, set: value => emit('update:publishOpen', value) }), capability = ref<PublicationCapability | null>(null), capabilityError = ref(false);
const shortHead = computed(() => props.task.snapshot.head_sha.slice(0,8));
// 页面工具栏已显示标题，正文去掉重复的一级标题；导出文件仍保留。
const displayReport = computed(() => props.report.replace(/^#\s+Rivu 审查(?:评论|报告)\s*(?:\r?\n)+/, ''));
const commentMarkdown = computed(() => props.plan?.review_comment_markdown || props.plan?.summary_markdown || '');
const copyState = ref<'idle' | 'copied' | 'failed'>('idle');
let copyTimer: ReturnType<typeof setTimeout> | null = null;
async function copyComment() {
  if (!commentMarkdown.value) return;
  try { await navigator.clipboard.writeText(commentMarkdown.value); copyState.value = 'copied'; }
  catch { copyState.value = 'failed'; }
  if (copyTimer) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => { copyState.value = 'idle'; }, 2000);
}
onBeforeUnmount(() => { if (copyTimer) clearTimeout(copyTimer); });
async function loadCapability() { capabilityError.value = false;
try { capability.value = await getPublicationCapability(); } catch { capabilityError.value = true; } } onMounted(() => { if (!props.task.source) void loadCapability(); });
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
<ReportSummaryBar v-if="summary" :summary="summary" />
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
<button class="btn secondary" :disabled="!commentMarkdown" :title="commentMarkdown ? (task.source ? '复制本地快照的审查总结' : '复制将发布到 PR 的总结评论原文') : '评论尚未生成'" @click="copyComment">
<AppIcon name="copy" />{{ copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制评论' }}</button>
<button v-if="!task.source" class="btn secondary" :aria-expanded="showPublish" @click="showPublish = !showPublish">
<AppIcon name="send" />{{ showPublish ? '收起发布面板' : '发布到 GitHub' }}</button>
<span class="sr-only" role="status">{{ copyState === 'copied' ? '评论已复制到剪贴板' : copyState === 'failed' ? '复制失败，请重试' : '' }}</span>
</div>
</div>
<div class="rd-document">
<p v-if="loading" class="muted">正在读取报告…</p>
<ReportMarkdown v-else-if="report" :content="displayReport" />
<p v-else class="muted">尚无可读取的 Markdown 报告。</p>
</div>
</div>
<GitHubPublicationPanel v-if="!task.source && showPublish && plan && terminal(task.status)" :review-id="task.id" :plan="plan" :capability="capability" :capability-error="capabilityError" @retry-capability="loadCapability" @result="() => {}" />
</div>
</template>
