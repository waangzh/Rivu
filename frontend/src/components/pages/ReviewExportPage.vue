<script setup lang="ts">
import { ref } from 'vue';
import type { Run } from '../../types/review';
import type { ExportFormat } from '../../types/export';
import { exportReview } from '../../api/client';
import { exportFormatLabel } from '../../utils/exportPresentation';
const props = defineProps<{ task: Run }>();
const format = ref<ExportFormat>('markdown');
const filename = ref('');
const state = ref<'idle' | 'loading' | 'done' | 'failed'>('idle');
async function download() {
  state.value = 'loading';
  try {
    const blob = await exportReview(props.task.id, {
      format: format.value,
      filename: filename.value || null,
      severities: [],
      paths: [],
      page: 1,
      page_size: 100,
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename.value || `rivu-${props.task.id.slice(0, 8)}.${format.value}`;
    anchor.click();
    URL.revokeObjectURL(url);
    state.value = 'done';
  } catch {
    state.value = 'failed';
  }
}
</script>
<template>
<div class="page-head">
<div>
<h2>导出</h2>
<p class="title-sub">把已保存的审查发现导出为表格、数据或 Markdown。</p>
</div>
<span class="badge neutral">{{ exportFormatLabel(format) }}</span>
</div>
<section class="card card-pad export-panel">
<label>
<span>格式</span>
<select v-model="format" class="input">
<option value="markdown">Markdown 报告</option>
<option value="csv">CSV 表格</option>
<option value="json">JSON 数据</option>
</select>
</label>
<label>
<span>文件名（可选）</span>
<input v-model="filename" class="input" placeholder="rivu-review.md" />
</label>
<button class="btn primary" :disabled="state === 'loading'" @click="download">
{{ state === 'loading' ? '正在生成…' : '导出' }}
</button>
<p v-if="state === 'done'" class="muted">导出已开始下载。</p>
<p v-else-if="state === 'failed'" class="notice">导出失败，请重试。</p>
</section>
</div>
</template>
<style scoped src="../../styles/review-export.css"></style>
