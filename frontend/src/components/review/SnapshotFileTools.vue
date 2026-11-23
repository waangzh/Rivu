<script setup lang="ts">
import { ref } from 'vue';
import type { Run } from '../../types/review';
import { getSnapshotFile, searchSnapshot } from '../../api/client';
import { parseSnapshotHits, snapshotHitLabel } from '../../utils/snapshotTools';
const props = defineProps<{ task: Run }>();
const path = ref('');
const query = ref('');
const content = ref('');
const hits = ref<string[]>([]);
const error = ref('');
const loading = ref(false);
async function openFile() {
  loading.value = true;
  error.value = '';
  try {
    const file = await getSnapshotFile(props.task.id, path.value);
    content.value = file.content;
  } catch {
    error.value = '无法读取该快照文件。';
  } finally {
    loading.value = false;
  }
}
async function search() {
  loading.value = true;
  error.value = '';
  try {
    const payload = await searchSnapshot(props.task.id, query.value, path.value || '.');
    hits.value = parseSnapshotHits(payload).map(snapshotHitLabel);
  } catch {
    error.value = '检索失败，请检查查询与路径。';
  } finally {
    loading.value = false;
  }
}
</script>
<template>
<section class="card card-pad snapshot-tools">
<h3>快照文件工具</h3>
<p class="muted">读取固定快照中的文件，或在本机检索关键词。</p>
<div class="snapshot-tools__row">
<input v-model="path" class="input" placeholder="相对路径，例如 src/main.py" />
<button class="btn secondary" :disabled="loading || !path" @click="openFile">读取文件</button>
</div>
<div class="snapshot-tools__row">
<input v-model="query" class="input" placeholder="检索关键词" />
<button class="btn secondary" :disabled="loading || !query" @click="search">检索</button>
</div>
<p v-if="error" class="notice">{{ error }}</p>
<pre v-if="content" class="snapshot-tools__content">{{ content }}</pre>
<ul v-if="hits.length" class="snapshot-tools__hits">
<li v-for="hit in hits" :key="hit">{{ hit }}</li>
</ul>
</section>
</template>
