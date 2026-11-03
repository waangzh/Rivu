<script setup lang="ts">
import { computed } from 'vue';
import type { PendingReviewCreation } from '../../utils/reviewCreation';
import { formatElapsed } from '../../utils/reviewProgress';
import { useReviewClock } from '../../composables/useReviewClock';
import AppIcon from '../common/AppIcon.vue';

const props = withDefaults(defineProps<{
  creation: PendingReviewCreation;
  submitting: boolean;
  rejected: boolean;
  error: string;
  recoverable?: boolean;
  publicationToken?: string;
}>(), { recoverable: true });
defineEmits<{ retry: []; edit: []; history: []; 'update:publicationToken': [value: string] }>();
const now = useReviewClock();
const elapsed = computed(() => formatElapsed(props.creation.startedAt, now.value));
const title = computed(() => props.submitting ? '正在创建审查' : props.rejected ? '未能创建审查' : '尚未确认创建结果');
</script>

<template>
  <section class="review-submission card card-pad" aria-labelledby="submission-title">
    <div class="progress-eyebrow"><span v-if="submitting" class="rg-live-dot" /><AppIcon v-else name="alert" />审查进度</div>
    <h1 id="submission-title" tabindex="-1">{{ title }}</h1>
    <p class="progress-lead" role="status">{{ submitting ? '正在提交请求并等待服务端返回审查记录。' : rejected ? '服务端未接受这次请求，请检查原因后修改。' : '连接中断或服务暂不可用，任务可能已经创建。请继续确认这次请求。' }}</p>
    <p class="submission-pr"><AppIcon name="branch" /><span>{{ creation.input.source ? creation.input.source.repository_key + ': ' + creation.input.source.base_ref + ' → ' + creation.input.source.head_ref : creation.input.pr_url }}</span></p>
    <div class="progress-facts"><span><AppIcon name="clock" />等待 {{ elapsed }}</span><span>尚未取得任务 ID</span></div>
    <p v-if="error" class="notice warn space-top" role="alert">{{ error }}</p>
    <p v-if="submitting" class="muted space-top">创建请求包含获取 PR、固定源码版本和准备审查范围，大型仓库可能需要更长时间。取得任务 ID 后会自动显示执行进度。</p>
    <p v-if="!recoverable" class="notice warn space-top">当前浏览器无法保存请求标识。请保留此页，连接恢复后继续确认这次请求。</p>
    <p v-else-if="submitting && now - Date.parse(creation.startedAt) >= 15000" class="notice space-top">仍在等待创建结果。此时无法判断具体准备阶段，请勿重复新建。刷新页面后会使用同一请求标识继续确认。</p>
    <div v-if="creation.input.auto_publish && !submitting && !rejected" class="submission-publish">
      <div class="submission-publish__head">
        <span class="submission-publish__icon"><AppIcon name="shield" :size="16" /></span>
        <div>
          <strong>重新输入发布操作口令以继续确认</strong>
          <p>刷新页面不会保留口令；沿用原请求标识，不会重复创建任务。</p>
        </div>
      </div>
      <label class="field-label" for="submission-publication-token">发布操作口令
        <input id="submission-publication-token" class="input" type="password" autocomplete="off" :value="publicationToken ?? ''"
          @input="$emit('update:publicationToken', ($event.target as HTMLInputElement).value)" />
      </label>
    </div>
    <div class="progress-actions space-top">
      <button v-if="!submitting && !rejected" class="btn primary" :disabled="creation.input.auto_publish && !publicationToken" @click="$emit('retry')">继续确认这次请求<AppIcon name="arrow-right" /></button>
      <button v-if="rejected" class="btn primary" @click="$emit('edit')">返回修改</button>
      <button class="btn secondary" @click="$emit('history')">查看审查历史</button>
    </div>
  </section>
</template>

<style scoped>
.submission-publish { display: grid; gap: 12px; margin-top: var(--section-gap); padding: 16px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface2); }
.submission-publish__head { display: flex; align-items: flex-start; gap: 10px; }
.submission-publish__icon { display: grid; place-items: center; flex: 0 0 28px; width: 28px; height: 28px; border-radius: 8px; background: var(--blueWash); color: var(--primary); }
.submission-publish__head strong { display: block; color: var(--ink); font-size: .9375rem; font-weight: 650; line-height: 1.5; }
.submission-publish__head p { margin: 3px 0 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.submission-publish .field-label { margin: 0; }
</style>
