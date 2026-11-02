<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getPublicationCapability } from '../../../api/client';

defineProps<{ modelValue: boolean; token: string; disabled?: boolean }>();
defineEmits<{ 'update:modelValue': [value: boolean]; 'update:token': [value: string] }>();
const enabled = ref(false), loading = ref(true), failed = ref(false);
const controller = new AbortController();
async function load() {
  loading.value = true;
  failed.value = false;
  try {
    const result = await getPublicationCapability(controller.signal);
    if (!controller.signal.aborted) enabled.value = result.enabled;
  } catch {
    if (!controller.signal.aborted) failed.value = true;
  } finally {
    if (!controller.signal.aborted) loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => controller.abort());
</script>

<template>
  <fieldset class="automatic-publication-options" :disabled="disabled">
    <legend class="field-label">结果发布</legend>
    <label class="check-row">
      <input type="checkbox" :checked="modelValue" :disabled="loading || failed || !enabled"
        @change="$emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      本次审查完成后自动发布到 GitHub PR
    </label>
    <p v-if="loading" class="muted small" role="status">正在检查发布能力…</p>
    <p v-else-if="failed" class="muted small">发布能力读取失败。<button type="button" class="link" @click="load">重新检查</button></p>
    <p v-else-if="!enabled" class="muted small">服务端未启用 GitHub 发布，仍可正常审查并复制结果。</p>
    <template v-if="modelValue">
      <p class="muted small">授权仅用于本次 PR 的固定版本：更新一条摘要并发布已核验的代码意见。不完整结果转人工确认。</p>
      <label class="field-label space-top">发布操作口令
        <input class="input" type="password" autocomplete="off" :value="token"
          placeholder="独立操作口令，不是 GitHub PAT"
          @input="$emit('update:token', ($event.target as HTMLInputElement).value)" />
      </label>
      <p class="muted small">口令仅保留在页面内存中；提交即授权本次任务按上述条件自动发布。</p>
    </template>
  </fieldset>
</template>

<style scoped>
.automatic-publication-options { border: 0; padding: 0; margin: 24px 0 0; min-width: 0; }
.automatic-publication-options legend { padding: 0; margin-bottom: 12px; }
</style>
