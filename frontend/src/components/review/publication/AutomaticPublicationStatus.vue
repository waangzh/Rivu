<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getAutomaticPublication } from '../../../api/client';
import type { AutomaticPublicationStatus } from '../../../types/publication';
import { automaticPublicationStateLabel, publicationErrorText } from '../../../utils/publicationPresentation';

const props = defineProps<{ reviewId: string }>();
defineEmits<{ inspect: [] }>();
const status = ref<AutomaticPublicationStatus | null>(null), failed = ref(false);
const controller = new AbortController();
let timer: ReturnType<typeof setTimeout> | null = null;
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
  <aside v-if="status || failed" class="notice space-bottom automatic-publication-status" aria-label="自动发布状态" role="status">
    <template v-if="status">
      <strong>{{ automaticPublicationStateLabel[status.state] }}</strong>
      <p v-if="status.last_error_code" class="small">{{ publicationErrorText(status.last_error_code) }}</p>
      <p v-if="status.state === 'waiting_review'" class="small">授权已保存，关闭页面不会取消；不完整结果将等待人工确认。</p>
      <p v-if="status.state === 'uncertain'" class="small">先核对远端状态，不会自动重复发送评论。</p>
      <button v-if="!['waiting_review', 'queued', 'preparing', 'publishing', 'paused'].includes(status.state)"
        type="button" class="link" @click="$emit('inspect')">查看发布详情</button>
    </template>
    <p v-if="failed" class="small">暂时无法读取自动发布状态，正在重试；不能据此判断发布成功。</p>
  </aside>
</template>

<style scoped>
.automatic-publication-status { display: block; }
.automatic-publication-status p, .automatic-publication-status button { margin-top: 8px; }
</style>
