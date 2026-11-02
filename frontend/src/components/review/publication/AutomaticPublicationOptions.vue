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
  <fieldset class="automatic-publication" :disabled="disabled">
    <legend class="sr-only">结果发布</legend>
    <header class="automatic-publication__head">
      <h3>结果发布</h3>
      <p>审查结束后由服务端写入 PR；不完整结果转人工确认。</p>
    </header>
    <label class="automatic-publication__option" :class="{ selected: modelValue }">
      <input type="checkbox" :checked="modelValue" :disabled="loading || failed || !enabled"
        @change="$emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      <span>
        <strong>本次审查完成后自动发布到 GitHub PR</strong>
        <small>更新一条总结评论，并发布已核验的代码意见。</small>
      </span>
    </label>
    <p v-if="loading" class="automatic-publication__status" role="status">正在检查发布能力…</p>
    <p v-else-if="failed" class="automatic-publication__status">发布能力读取失败。<button type="button" class="link" @click="load">重新检查</button></p>
    <p v-else-if="!enabled" class="automatic-publication__status">服务端未启用 GitHub 发布，仍可正常审查并复制结果。</p>
    <div v-if="modelValue" class="automatic-publication__auth">
      <label class="field-label">发布操作口令
        <input class="input" type="password" autocomplete="off" :value="token"
          placeholder="独立操作口令，不是 GitHub PAT"
          @input="$emit('update:token', ($event.target as HTMLInputElement).value)" />
      </label>
      <p class="automatic-publication__hint">授权仅用于本次 PR 的固定版本：更新一条总结评论并发布已核验的代码意见，不完整结果转人工确认。口令仅保留在页面内存中，提交即授权本次任务按上述条件自动发布。</p>
    </div>
  </fieldset>
</template>

<style scoped>
.automatic-publication { min-width: 0; margin: 22px 0 0; padding: 22px 0 0; border: 0; border-top: 1px solid var(--line); }
.automatic-publication__head h3 { margin: 0; font-size: 1rem; font-weight: 650; line-height: 1.5; }
.automatic-publication__head p { margin: 4px 0 0; color: var(--muted); font-size: .875rem; line-height: 1.7; }
.automatic-publication__option { display: flex; align-items: flex-start; gap: 10px; margin-top: 14px; padding: 12px 14px; border: 1px solid var(--line2); border-radius: var(--radius-control); background: var(--surface); color: var(--ink); cursor: pointer; transition: border-color var(--speed), background var(--speed); }
.automatic-publication__option:hover { border-color: var(--primary); }
.automatic-publication__option.selected { border-color: color-mix(in srgb, var(--primary) 42%, var(--line)); background: var(--blueWash); }
.automatic-publication__option input { flex: none; width: 16px; height: 16px; min-height: 0; margin: 3px 0 0; padding: 0; accent-color: var(--primary); }
.automatic-publication__option strong { display: block; font-size: .9375rem; font-weight: 650; line-height: 1.5; }
.automatic-publication__option small { display: block; margin-top: 3px; color: var(--muted); font-size: .8125rem; line-height: 1.6; }
.automatic-publication__status { margin: 10px 0 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.automatic-publication__auth { display: grid; gap: 10px; margin-top: 12px; padding: 14px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface2); }
.automatic-publication__auth .field-label { margin: 0; }
.automatic-publication__hint { margin: 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
</style>
