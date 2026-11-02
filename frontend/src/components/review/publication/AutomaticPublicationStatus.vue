<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { getAutomaticPublication } from '../../../api/client';
import type { AutomaticPublicationStatus } from '../../../types/publication';
import { automaticPublicationStateLabel, publicationErrorText } from '../../../utils/publicationPresentation';
import AppIcon from '../../common/AppIcon.vue';

const props = defineProps<{ reviewId: string }>();
defineEmits<{ inspect: [] }>();
const status = ref<AutomaticPublicationStatus | null>(null), failed = ref(false);
const controller = new AbortController();
let timer: ReturnType<typeof setTimeout> | null = null;
const tone = computed(() => {
  const state = status.value?.state;
  if (!state) return 'warning';
  if (state === 'succeeded') return 'success';
  if (state === 'failed' || state === 'blocked') return 'danger';
  if (['partial', 'uncertain', 'stale', 'manual_required'].includes(state)) return 'warning';
  return 'info';
});
const icon = computed(() => tone.value === 'success' ? 'check-circle' : tone.value === 'info' ? 'clock' : 'alert');
async function load() {
  try {
    const result = await getAutomaticPublication(props.reviewId, controller.signal);
    if (controller.signal.aborted) return;
    status.value = result;
    failed.value = false;
    if (result && ['waiting_review', 'queued', 'preparing', 'publishing', 'paused'].includes(result.state)) {
      timer = setTimeout(load, 1500);
    }
  } catch {
    if (!controller.signal.aborted) {
      failed.value = true;
      timer = setTimeout(load, 3000);
    }
  }
}
onMounted(load);
onBeforeUnmount(() => { controller.abort(); if (timer) clearTimeout(timer); });
</script>

<template>
  <aside v-if="status || failed" class="auto-publication-status" :data-tone="tone" aria-label="自动发布状态" role="status">
    <span class="auto-publication-status__icon" aria-hidden="true"><AppIcon :name="icon" :size="16" /></span>
    <div class="auto-publication-status__body">
      <span class="auto-publication-status__label">自动发布</span>
      <strong>{{ status ? automaticPublicationStateLabel[status.state] : '自动发布状态读取失败' }}</strong>
      <p v-if="status?.last_error_code">{{ publicationErrorText(status.last_error_code) }}</p>
      <p v-if="status?.state === 'waiting_review'">授权已保存，关闭页面不会取消；不完整结果将等待人工确认。</p>
      <p v-if="status?.state === 'uncertain'">先核对远端状态，不会自动重复发送评论。</p>
      <p v-if="failed">暂时无法读取自动发布状态，正在重试；不能据此判断发布成功。</p>
    </div>
    <button v-if="status && !['waiting_review', 'queued', 'preparing', 'publishing', 'paused'].includes(status.state)"
      type="button" class="button button--secondary button--compact" @click="$emit('inspect')">查看发布详情</button>
  </aside>
</template>

<style scoped>
.auto-publication-status { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px; margin-bottom: var(--section-gap); padding: 12px 14px; border: 1px solid var(--line); border-left-width: 3px; border-radius: var(--radius); background: var(--surface2); }
.auto-publication-status[data-tone="info"] { border-color: color-mix(in srgb, var(--primary) 18%, var(--line)); border-left-color: var(--primary); background: var(--blueWash); }
.auto-publication-status[data-tone="success"] { border-color: color-mix(in srgb, var(--success) 22%, var(--line)); border-left-color: var(--success); background: var(--successWash); }
.auto-publication-status[data-tone="warning"] { border-color: color-mix(in srgb, var(--amber) 22%, var(--line)); border-left-color: var(--amber); background: var(--amberWash); }
.auto-publication-status[data-tone="danger"] { border-color: color-mix(in srgb, var(--danger) 22%, var(--line)); border-left-color: var(--danger); background: var(--dangerWash); }
.auto-publication-status__icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: var(--surface); color: var(--primary); }
.auto-publication-status[data-tone="success"] .auto-publication-status__icon { color: var(--success); }
.auto-publication-status[data-tone="warning"] .auto-publication-status__icon { color: var(--amber); }
.auto-publication-status[data-tone="danger"] .auto-publication-status__icon { color: var(--danger); }
.auto-publication-status__body { min-width: 0; }
.auto-publication-status__label { display: block; color: var(--muted); font-size: 12px; font-weight: 600; line-height: 1.4; }
.auto-publication-status__body strong { display: block; margin-top: 1px; color: var(--ink); font-size: .9375rem; font-weight: 650; line-height: 1.5; }
.auto-publication-status__body p { margin: 3px 0 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; overflow-wrap: anywhere; }
.auto-publication-status .button { flex: 0 0 auto; white-space: nowrap; }
@media (max-width: 600px) {
  .auto-publication-status { grid-template-columns: auto minmax(0, 1fr); }
  .auto-publication-status .button { grid-column: 2; justify-self: start; }
}
</style>
