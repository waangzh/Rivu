<script setup lang="ts">
import { computed } from 'vue';
import type { ReviewReportResponse } from '../../types/review';
const props = defineProps<{ summary: ReviewReportResponse }>();
const duration = computed(() => props.summary.duration_seconds == null
  ? '未知'
  : `${props.summary.duration_seconds} 秒`);
</script>
<template>
<div class="report-summary-bar">
<span>{{ summary.findings_count }} 条已确认发现</span>
<span>审查耗时 {{ duration }}</span>
</div>
</template>
