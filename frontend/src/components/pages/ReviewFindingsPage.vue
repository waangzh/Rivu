<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Candidate, Finding, Run } from '../../types/review';
import AppIcon from '../common/AppIcon.vue';
import EmptyState from '../common/EmptyState.vue';
import StatusBadge from '../common/StatusBadge.vue';

const props = defineProps<{
  task: Run;
  findings: Finding[];
  candidates: Candidate[];
  loading?: boolean;
}>();

const query = ref('');
const selected = ref('');

const visible = computed(() => props.findings.filter(item => (
  item.claim + ' ' + (item.location?.path || '')
).toLowerCase().includes(query.value.toLowerCase())));
const unresolvedCandidates = computed(() => props.candidates.filter(
  item => item.admission_reason || !item.verification || item.verification.verdict === 'insufficient',
));
const current = computed(() => visible.value.find(item => item.id === selected.value) || visible.value[0]);

const attribution = {
  introduced: '本次变更引入',
  exposed: '本次变更暴露或扩大影响',
  pre_existing: '既有问题（非本次新增）',
  uncertain: '归因尚未确定',
};

watch(() => props.task.id, () => { selected.value = ''; });
</script>

<template>
  <div class="page-head">
    <div>
      <h2>问题与证据</h2>
      <p class="title-sub">已确认问题均经过源码证据检查与独立核验；疑似问题和证据不足项单独保留。</p>
    </div>
    <span class="badge neutral">{{ findings.length }} 条 Finding</span>
  </div>

  <div class="workspace-toolbar rg-finding-filters">
    <input v-model="query" class="input" placeholder="搜索问题或文件" aria-label="搜索问题" />
    <span class="muted small">疑似问题记录 {{ candidates.length }} 条</span>
  </div>

  <section v-if="unresolvedCandidates.length" class="card card-pad space-top">
    <div class="card-header">
      <h2>待确认疑似问题</h2>
      <span class="badge neutral">{{ unresolvedCandidates.length }} 条</span>
    </div>
    <p class="muted small">以下记录未计入已确认问题；复核结果校验失败与业务前提不足分别标明，均不等于不存在缺陷。展开每条记录可查看核验说明。</p>
    <ol class="rg-candidate-list">
      <li v-for="item in unresolvedCandidates" :key="item.id">
        <details class="rg-candidate">
          <summary>
            <span class="rg-candidate__body">
              <strong>{{ item.proposal.claim }}</strong>
              <span class="rg-candidate__meta">
                <code v-if="item.proposal.location">{{ item.proposal.location.path }}:{{ item.proposal.location.line }} · {{ item.proposal.location.side === 'base' ? 'BASE' : 'HEAD' }}</code>
                <span>来源证据 {{ item.sources.length }} 条</span>
                <span v-if="item.verification_sources.length">核验引用 {{ item.verification_sources.length }} 条</span>
              </span>
            </span>
            <span class="rg-candidate__aside">
              <StatusBadge :status="item.verification ? 'insufficient' : 'unverified'" :label="item.admission_reason ? '复核结果校验失败' : undefined" />
              <AppIcon name="chevron-down" :size="16" />
            </span>
          </summary>
          <p v-if="item.admission_reason" class="rg-candidate__reason">
            复核结果未通过系统校验（{{ item.admission_reason }}），不能作为已确认问题；请检查引用或复核交付。这不表示源码一定缺失。
          </p>
          <p v-else-if="item.verification?.question" class="rg-candidate__reason">
            源码观察：{{ item.verification.question.observation }}<br />
            缺失前提：{{ item.verification.question.missing_premise }}<br />
            请确认：{{ item.verification.question.question }}
          </p>
          <p class="rg-candidate__reason">
            {{ item.verification ? '核验说明：' + item.verification.reason : '本轮结束时尚未取得核验结果。' }}
          </p>
        </details>
      </li>
    </ol>
  </section>

  <p v-if="loading" class="muted space-top" role="status">正在读取问题记录…</p>
  <section v-else-if="!visible.length" class="card card-pad space-top">
    <EmptyState
      icon="shield"
      title="没有匹配的已确认问题"
      :description="candidates.length ? '请同时查看以上待确认疑似问题的核验记录，当前列表不能证明不存在缺陷。' : '未发现问题不能证明审查单元覆盖完整。'"
    />
  </section>

  <div v-else class="rg-restored-findings space-top">
    <aside class="card card-pad rg-findings-list">
      <button v-for="item in visible" :key="item.id" class="rg-finding-row" :class="{ active: current?.id === item.id }" @click="selected = item.id">
        <span class="rg-inline-badges"><StatusBadge :status="item.severity" /></span>
        <strong>{{ item.claim }}</strong>
        <code>{{ item.location?.path || '位置尚未确定' }}</code>
      </button>
    </aside>

    <article v-if="current" class="section-stack">
      <section class="card card-pad">
        <div class="card-header">
          <h2>{{ current.claim }}</h2>
          <StatusBadge :status="current.severity" />
        </div>
        <p v-if="current.location" class="small">
          <code>{{ current.location.path }}:{{ current.location.line }}</code>
          · {{ current.location.side === 'base' ? 'BASE' : 'HEAD' }}
        </p>
        <dl class="rg-finding-facts">
          <div><dt>触发条件</dt><dd>{{ current.trigger }}</dd></div>
          <div><dt>影响</dt><dd>{{ current.impact }}</dd></div>
          <div><dt>审查意见</dt><dd class="rg-preserve-lines">{{ current.author_comment }}</dd></div>
        </dl>
        <p class="small">变更归因：{{ attribution[current.attribution] }}</p>
        <details class="space-top">
          <summary class="link">来源证据</summary>
          <code v-for="source in current.sources" :key="source.id" class="rg-evidence-id">{{ source.side.toUpperCase() }} · {{ source.path }}:{{ source.start_line }}–{{ source.end_line }} · {{ source.id }}</code>
        </details>
        <p v-if="!current.publishable" class="notice small">该 Finding 不具备发布资格，发布面板会按服务端计划进一步校验。</p>
      </section>
    </article>
  </div>
</template>
