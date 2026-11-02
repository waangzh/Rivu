<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { PublicationPlan, PublicationPreflightResult, PublicationAttempt, PublicationFinding } from "../../../types/publication";
import type { PublicationCapability } from "../../../types/operations";
import { preflightPublication, createPublication, getPublications, getPublication, retryPublication } from "../../../api/client";
import { publicationFindingTitle } from "../../../utils/reportPresentation";
import { publicationErrorText, publicationItemKindLabel, publicationItemStateLabel,
  publicationStateLabel, publicationWarningText, inlineActionLabel } from "../../../utils/publicationPresentation";
import StatusBadge from "../../common/StatusBadge.vue";
import AppIcon from "../../common/AppIcon.vue";

const props = defineProps<{ reviewId: string; plan: PublicationPlan; capability: PublicationCapability | null; capabilityError: boolean }>();
const emit = defineEmits<{
  result: [value: { tone: "success" | "warn" | "muted"; text: string } | null];
  "retry-capability": [];
}>();
const token = ref("");
const selected = ref<string[]>([]);
const preview = ref<PublicationPreflightResult | null>(null);
const history = ref<PublicationAttempt[]>([]);
const historyLoaded = ref(false);
const current = ref<PublicationAttempt | null>(null);
const busy = ref(false);
const error = ref("");
const acknowledge = ref(false);
const dialog = ref<HTMLDialogElement | null>(null);
const confirmationKey = ref("");
const expandAll = ref(false);
let controller: AbortController | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const findings = computed(() => [...props.plan.inline_drafts, ...props.plan.summary_findings].sort((left, right) =>
    ["critical", "high", "medium", "low"].indexOf(left.severity) - ["critical", "high", "medium", "low"].indexOf(right.severity)
    || (left.anchor?.file_path ?? "").localeCompare(right.anchor?.file_path ?? "")
    || (left.anchor?.start_line ?? 0) - (right.anchor?.start_line ?? 0)));
const findingByKey = computed(() => new Map(findings.value.map(item => [item.finding_key, item])));
const severityOf = (key: string): PublicationFinding["severity"] => findingByKey.value.get(key)?.severity ?? "low";
const summaryHtml = computed(() => renderMarkdown(preview.value?.summary_markdown || ""));
const canConfirm = computed(() => !!preview.value?.ready && !busy.value
  && (!preview.value.requires_partial_acknowledgement || acknowledge.value));
const publishEnabled = computed(() => props.capability?.enabled === true);

function renderMarkdown(value: string) {
  return DOMPurify.sanitize(marked.parse(value, { async: false, gfm: true }) as string,
    { USE_PROFILES: { html: true }, FORBID_TAGS: ["style"] });
}
function clearTimer() { if (timer) clearTimeout(timer); timer = null; }
function invalidate() { preview.value = null; acknowledge.value = false; confirmationKey.value = ""; dialog.value?.close(); }
watch(() => JSON.stringify(props.plan), (value, previous) => {
  generation += 1; controller?.abort(); clearTimer(); current.value = null; history.value = []; historyLoaded.value = false; busy.value = false;
  selected.value = findings.value.map(item => item.finding_key); token.value = ""; error.value = ""; expandAll.value = false;
  invalidate();
if (previous !== undefined) emit("result", null);
}, { immediate: true });
watch(selected, invalidate, { deep: true });
watch(token, () => { generation += 1; controller?.abort(); clearTimer(); busy.value = false; invalidate(); });
watch(current, value => {
  if (!value) { emit("result", null);
return; }
  if (value.state === "succeeded") {
    const inline = value.items.filter(item => item.kind === "inline" && item.state === "published").length;
    emit("result", { tone: "success", text: inline ? `已发布总结及 ${inline} 条代码意见` : "已发布总结评论" });
  } else if (["partial", "failed", "uncertain", "stale"].includes(value.state)) {
    emit("result", { tone: "warn", text: publicationStateLabel[value.state] });
  } else emit("result", { tone: "muted", text: publicationStateLabel[value.state] });
});
onBeforeUnmount(() => { generation += 1; controller?.abort(); clearTimer(); });

async function action(operation: (signal: AbortSignal) => Promise<void>) {
  controller?.abort(); clearTimer(); controller = new AbortController();
  const signal = controller.signal, version = ++generation;
  busy.value = true; error.value = "";
  try { await operation(signal); }
  catch (failure) { if (!signal.aborted && version === generation) error.value = failure instanceof Error ? failure.message : "发布请求失败"; }
  finally { if (!signal.aborted && version === generation) busy.value = false; }
}
function schedulePoll(attempt: PublicationAttempt) {
  current.value = attempt;
  if (["queued", "preparing", "publishing"].includes(attempt.state)) timer = setTimeout(() => void refresh(attempt.id), 1500);
}
async function refresh(identity: string) {
  await action(async signal => {
    const result = await getPublication(props.reviewId, identity, token.value, signal);
    if (!signal.aborted) schedulePoll(result);
  });
}
async function preflight() {
  invalidate();
  await action(async signal => {
    const result = await preflightPublication(props.reviewId, selected.value, token.value, signal);
    if (!signal.aborted) { preview.value = result; confirmationKey.value = crypto.randomUUID(); }
  });
}
async function submit() {
  if (!canConfirm.value || !preview.value) return;
  const prepared = preview.value;
  dialog.value?.close();
  current.value = null;
  await action(async signal => {
    const accepted = await createPublication(props.reviewId, { expected_plan_hash: prepared.plan_hash, expected_report_version: prepared.report_version, expected_head_sha: prepared.head_sha,
      publish_summary: true, finding_keys: [...selected.value], acknowledge_partial_review: acknowledge.value }, confirmationKey.value, token.value, signal);
    if (signal.aborted) return;
    const result = await getPublication(props.reviewId, accepted.publication_id, token.value, signal);
    if (!signal.aborted) schedulePoll(result);
  });
}
async function loadHistory() {
  await action(async signal => { const result = await getPublications(props.reviewId, token.value, signal);
if (!signal.aborted) { history.value = result; historyLoaded.value = true; } });
}
async function recover() {
  if (!current.value) return;
  const identity = current.value.id;
  await action(async signal => { const result = await retryPublication(props.reviewId, identity, token.value, signal);
if (!signal.aborted) schedulePoll(result); });
}
function safeLink(value: string | null) {
  try { const url = new URL(value || "");
return url.protocol === "https:" && url.hostname === "github.com" ? url.href : undefined; }
  catch { return undefined; }
}
const placementLabel = (placement: string) => placement === "inline" ? "代码行评论" : "总结评论";
const highPriority = (key: string) => ["critical", "high"].includes(severityOf(key));
function locationText(finding: PublicationFinding) {
  const anchor = finding.anchor;
  return `${anchor.file_path}${anchor.start_line ? `:${anchor.start_line}` : ""} · ${anchor.side === "base" ? "Base" : "Head"}`;
}
function stateTone(state: string) {
  if (state === "succeeded") return "success";
  if (state === "failed") return "danger";
  if (["partial", "uncertain", "stale"].includes(state)) return "warning";
  return "info";
}
</script>

<template>
<section class="github-publication" aria-label="发布到 GitHub" :aria-busy="busy">
  <header class="github-publication__head">
    <h3>发布到 GitHub</h3>
    <p>由服务端使用独立发布身份写入 PR：更新总结评论并创建所选代码行评论。复制评论不受此限制。</p>
  </header>
  <template v-if="publishEnabled">
    <fieldset class="gp-step" :disabled="busy">
      <legend class="gp-step__title">1. 选择要发布的已核验问题（PR 总结评论始终发布）</legend>
      <p v-if="!findings.length" class="gp-note">没有合格问题草稿；预检将校验能否发布当前审查结论。</p>
      <div v-else class="gp-findings">
        <label v-for="finding in findings" :key="finding.finding_key" class="gp-finding"
          :class="{ 'is-selected': selected.includes(finding.finding_key) }">
          <input v-model="selected" type="checkbox" :value="finding.finding_key" />
          <span class="gp-finding__body">
            <strong>{{ publicationFindingTitle(finding) }}</strong>
            <span class="gp-finding__meta">
              <code>{{ locationText(finding) }}</code>
              <span>{{ placementLabel(finding.placement) }}</span>
            </span>
          </span>
          <StatusBadge :status="finding.severity" />
        </label>
      </div>
    </fieldset>
    <section class="gp-step">
      <h3 class="gp-step__title">2. 发布授权</h3>
      <div class="gp-authorize">
        <label class="gp-field">发布操作口令
          <input class="input" v-model="token" type="password" autocomplete="off" placeholder="独立操作口令，不是 GitHub Token" :disabled="busy" />
        </label>
        <button type="button" class="button button--secondary" :disabled="busy || !token" @click="preflight">检查发布条件并预览</button>
      </div>
      <p class="gp-note">预检不会写入 GitHub；确认前会重新核对 PR 与审查版本。</p>
    </section>
    <p v-if="busy" class="gp-status" role="status">正在核对发布条件及已有评论，必要时检查此前未确认的写入，请稍候…</p>
    <p v-if="error" class="gp-alert" role="alert">{{ error }}</p>
    <section v-if="preview" class="gp-step" aria-label="实际发布预览">
      <header class="gp-step__head">
        <h3 class="gp-step__title">3. 实际发布预览</h3>
        <code class="gp-revision">{{ preview.head_sha.slice(0, 8) }}</code>
      </header>
      <p class="gp-note">PR 总结评论{{ preview.summary_action === 'update' ? '将更新已有评论' : '将新建评论' }}，并创建下方所列代码行评论。</p>
      <div class="gp-metrics">
        <div><strong>{{ preview.inline.ready }}</strong><span>新建代码行评论</span></div>
        <div><strong>{{ preview.inline.already_published }}</strong><span>已存在相同评论，不重复发布</span></div>
        <div><strong>{{ preview.inline.summary_fallback }}</strong><span>无法定位，写入总结评论</span></div>
      </div>
      <article class="markdown-body gp-document" v-html="summaryHtml" />
      <div class="gp-inline-head">
        <h4>逐条代码意见</h4>
        <button v-if="preview.inline_previews.length" type="button" class="text-button" @click="expandAll = !expandAll">{{ expandAll ? '收起全部' : '展开全部' }}</button>
      </div>
      <details v-for="item in preview.inline_previews" :key="item.finding_key" class="gp-inline" :open="expandAll || highPriority(item.finding_key)">
        <summary>
          <code>{{ item.location?.path || '总结评论' }}</code>
          <span v-if="item.location" class="gp-inline__location">第 {{ item.location.line }} 行 · {{ item.location.side === 'LEFT' ? 'Base' : 'Head' }}</span>
          <StatusBadge :status="severityOf(item.finding_key)" />
          <span class="gp-inline__action">{{ inlineActionLabel[item.action] }}</span>
        </summary>
        <article class="markdown-body gp-inline__body" v-html="renderMarkdown(item.body_markdown)" />
      </details>
      <p v-for="warning in preview.warnings" :key="warning" class="gp-notice is-warning">{{ publicationWarningText(warning) }}</p>
      <label v-if="preview.requires_partial_acknowledgement" class="gp-ack"><input v-model="acknowledge" type="checkbox" />我已了解审查或发布不完整，以及未选择的问题不会发布，仍确认部分发布。</label>
      <div class="gp-actions gp-actions--end">
        <button type="button" class="button button--primary" :disabled="!canConfirm" @click="dialog?.showModal()">确认发布</button>
      </div>
    </section>
    <section v-if="current" class="gp-step" aria-label="发布结果">
      <header class="gp-step__head">
        <h3 class="gp-step__title">4. 发布结果</h3>
        <span class="gp-state" :data-tone="stateTone(current.state)">{{ publicationStateLabel[current.state] || '状态未知' }}</span>
      </header>
      <p v-if="current.last_error_code" class="gp-note">{{ publicationErrorText(current.last_error_code) }}</p>
      <p v-if="current.state === 'uncertain'" class="gp-alert">无法确认 GitHub 是否已收到评论，先检查远端状态；无法确认时会保持未知，需要人工处理，不会盲目重新发送。</p>
      <ul v-if="current.items.length" class="gp-items">
        <li v-for="item in current.items" :key="item.id">
          <span class="gp-item__kind">{{ publicationItemKindLabel(item.kind) }}</span>
          <span class="gp-item__state">{{ publicationItemStateLabel[item.state] || '状态未知' }}</span>
          <a v-if="safeLink(item.github_url)" :href="safeLink(item.github_url)" target="_blank" rel="noreferrer">在 GitHub 查看<AppIcon name="external" :size="12" /></a>
          <span v-if="item.last_error_code" class="gp-item__error">{{ publicationErrorText(item.last_error_code) }}</span>
        </li>
      </ul>
      <div v-if="['uncertain', 'partial', 'failed'].includes(current.state)" class="gp-actions">
        <button type="button" class="button button--secondary" :disabled="busy" @click="recover">{{ current.state === 'uncertain' ? '检查远端发布状态' : '重新检查并处理剩余项目' }}</button>
      </div>
    </section>
    <details class="gp-record">
      <summary>交付记录<AppIcon name="chevron-down" :size="14" /></summary>
      <div class="gp-record__body">
        <div class="gp-record__head">
          <p class="gp-note">发布后可在此回看每次投递的状态与远端结果。</p>
          <button type="button" class="button button--secondary button--compact" :disabled="busy || !token" @click="loadHistory">刷新交付记录</button>
        </div>
        <ul v-if="history.length" class="gp-record__list">
          <li v-for="item in history" :key="item.id">
            <button type="button" class="gp-record__row" @click="refresh(item.id)">
              <span class="gp-state" :data-tone="stateTone(item.state)">{{ publicationStateLabel[item.state] || '状态未知' }}</span>
              <time>{{ item.requested_at }}</time>
            </button>
          </li>
        </ul>
        <p v-else class="gp-note">{{ historyLoaded ? '尚无交付记录。发布后可在此查看每次投递的条目与远端链接。' : '输入发布操作口令并刷新，可查看每次投递的条目与远端链接。' }}</p>
      </div>
    </details>
    <dialog ref="dialog" class="gp-dialog" @click.self="dialog?.close()">
      <header><h3>确认发布到 GitHub</h3></header>
      <div class="gp-dialog__body">
        <p>将使用服务端发布身份{{ preview?.summary_action === 'update' ? '更新' : '新建' }} PR 总结评论，并创建 {{ preview?.inline.ready }} 条新代码行评论。</p>
        <p>审查版本：<code>{{ preview?.head_sha }}</code></p>
      </div>
      <footer>
        <button type="button" class="button button--secondary" @click="dialog?.close()">取消</button>
        <button type="button" class="button button--primary" :disabled="!canConfirm" @click="submit">确认发布到 GitHub</button>
      </footer>
    </dialog>
  </template>
  <template v-else-if="capability === null && !capabilityError"><p class="gp-status" role="status">正在检查服务端是否启用直接发布…</p></template>
  <template v-else>
    <div class="gp-step gp-fallback">
      <p class="gp-notice" :class="{ 'is-warning': capabilityError }" role="status">{{ capabilityError ? '暂时无法确认服务端发布能力。' : '服务端未启用直接发布。' }}完整评论仍可在上方阅读和复制，粘贴到 GitHub 即可。</p>
      <div v-if="capabilityError" class="gp-actions">
        <button type="button" class="button button--secondary button--compact" @click="$emit('retry-capability')">重新检查发布能力</button>
      </div>
      <details class="gp-enable">
        <summary>如何启用直接发布</summary>
        <p>在 <code>backend/.env</code> 配置独立发布凭据、仓库授权与操作口令，再设置 <code>RIVU_GITHUB_PUBLISH_ENABLED=true</code> 并重启后端。</p>
      </details>
    </div>
  </template>
</section>
</template>

<style scoped>
.github-publication { min-width: 0; margin: 16px 0 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); color: var(--ink); font-size: .875rem; line-height: 1.7; }
.github-publication__head { padding: 18px 22px; border-bottom: 1px solid var(--line); }
.github-publication__head h3 { margin: 0; font-size: 1rem; font-weight: 650; line-height: 1.5; }
.github-publication__head p { max-width: 78ch; margin: 4px 0 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.gp-step { display: grid; gap: 12px; min-width: 0; margin: 0; padding: 18px 22px; border: 0; border-bottom: 1px solid var(--line); }
.gp-step:last-child { border-bottom: 0; }
fieldset.gp-step { border-radius: 0; }
.gp-step__title { margin: 0; padding: 0; font-size: .9375rem; font-weight: 650; line-height: 1.5; color: var(--ink); }
.gp-step__head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.gp-note { margin: 0; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.gp-status { margin: 0; padding: 0 22px 18px; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.gp-alert { margin: 0; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--danger) 22%, var(--line)); border-radius: var(--radius-control); background: var(--dangerWash); color: var(--ink); font-size: .8125rem; line-height: 1.7; }
.gp-notice { margin: 0; padding: 11px 13px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface2); color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.gp-notice.is-warning { border-color: color-mix(in srgb, var(--amber) 22%, var(--line)); background: var(--amberWash); color: var(--ink); }
.gp-revision { flex: none; color: var(--muted); font-size: 12px; }
.gp-findings { overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-control); }
.gp-finding { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 11px 14px; background: var(--surface); color: var(--ink); cursor: pointer; transition: background var(--speed); }
.gp-finding + .gp-finding { border-top: 1px solid var(--line); }
.gp-finding:hover { background: var(--surface2); }
.gp-finding:not(.is-selected) { background: var(--surface2); color: var(--muted); }
.gp-finding input, .gp-ack input { flex: none; width: 16px; height: 16px; min-height: 0; margin: 0; padding: 0; accent-color: var(--primary); }
.gp-finding__body { display: grid; gap: 3px; min-width: 0; }
.gp-finding__body strong { color: inherit; font-size: .875rem; font-weight: 600; line-height: 1.6; overflow-wrap: anywhere; }
.gp-finding__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 10px; color: var(--muted); font-size: 12px; line-height: 1.6; }
.gp-finding__meta code { color: inherit; font-size: 12px; overflow-wrap: anywhere; }
.gp-authorize { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 10px; max-width: 640px; }
.gp-field { display: grid; gap: 7px; margin: 0; color: var(--ink); font-size: 13px; font-weight: 650; line-height: 1.5; }
.gp-field .input { font-weight: 400; }
.gp-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); }
.gp-metrics > div { min-width: 0; padding: 10px 14px; }
.gp-metrics > div + div { border-left: 1px solid var(--line); }
.gp-metrics strong { display: block; font-size: 20px; font-weight: 650; line-height: 1.25; font-variant-numeric: tabular-nums; }
.gp-metrics span { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; line-height: 1.5; }
.gp-document { max-height: 420px; overflow: auto; padding: 16px 18px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface2); }
.gp-document :deep(h1) { margin: 0 0 10px; font-size: 1.125rem; line-height: 1.5; letter-spacing: 0; }
.gp-document :deep(h2) { margin: 18px 0 8px; font-size: .9375rem; line-height: 1.5; }
.gp-document :deep(h3) { margin: 14px 0 6px; font-size: .875rem; line-height: 1.5; }
.gp-document :deep(p), .gp-inline__body :deep(p) { margin: 8px 0; font-size: .8125rem; line-height: 1.8; }
.gp-document :deep(ul), .gp-document :deep(ol), .gp-inline__body :deep(ul), .gp-inline__body :deep(ol) { margin: 8px 0; padding-left: 20px; }
.gp-document :deep(li), .gp-inline__body :deep(li) { margin: 5px 0; font-size: .8125rem; line-height: 1.75; }
.gp-document :deep(hr) { height: 0; margin: 14px 0; border: 0; border-top: 1px solid var(--line); }
.gp-document :deep(strong), .gp-inline__body :deep(strong) { color: var(--ink); font-weight: 650; }
.gp-document :deep(code), .gp-inline__body :deep(code) { padding: 1px 4px; border-radius: 4px; background: var(--surface); color: var(--ink); font-size: .9em; }
.gp-document :deep(blockquote) { margin: 10px 0; padding: 8px 12px; border-left: 3px solid var(--line2); color: var(--muted); }
.gp-inline__body :deep(p:first-child), .gp-inline__body :deep(h3:first-child) { margin-top: 12px; }
.gp-inline-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 2px; }
.gp-inline-head h4 { margin: 0; font-size: .875rem; font-weight: 650; }
.gp-inline { border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); }
.gp-inline + .gp-inline { margin-top: 8px; }
.gp-inline > summary { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 10px 14px; cursor: pointer; list-style: none; font-size: .8125rem; }
.gp-inline > summary::-webkit-details-marker { display: none; }
.gp-inline > summary::after { margin-left: auto; color: var(--muted); content: "›"; font-size: 16px; line-height: 1; transition: transform var(--speed); }
.gp-inline[open] > summary::after { transform: rotate(90deg); }
.gp-inline > summary code { font-size: 12px; }
.gp-inline__location, .gp-inline__action { color: var(--muted); font-size: 12px; }
.gp-inline__body { padding: 0 14px 14px; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.gp-ack { display: flex; align-items: flex-start; gap: 8px; padding: 11px 12px; border: 1px solid color-mix(in srgb, var(--amber) 22%, var(--line)); border-radius: var(--radius-control); background: var(--amberWash); color: var(--ink); font-size: .8125rem; line-height: 1.7; cursor: pointer; }
.gp-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.gp-actions--end { justify-content: flex-end; }
.gp-state { display: inline-flex; align-items: center; gap: 5px; flex: none; padding: 3px 9px; border-radius: 999px; background: var(--surface3); color: var(--muted); font-size: 12px; font-weight: 650; line-height: 1.4; }
.gp-state::before { width: 6px; height: 6px; border-radius: 50%; background: currentColor; content: ""; }
.gp-state[data-tone="success"] { background: var(--successWash); color: var(--success); }
.gp-state[data-tone="warning"] { background: var(--amberWash); color: var(--amber); }
.gp-state[data-tone="danger"] { background: var(--dangerWash); color: var(--danger); }
.gp-state[data-tone="info"] { background: var(--blueWash); color: var(--primary); }
.gp-items { margin: 0; padding: 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-control); list-style: none; }
.gp-items li { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; padding: 10px 14px; font-size: .8125rem; line-height: 1.6; }
.gp-items li + li { border-top: 1px solid var(--line); }
.gp-item__kind { color: var(--muted); }
.gp-item__state { font-weight: 650; }
.gp-items a { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; font-weight: 650; }
.gp-item__error { flex-basis: 100%; color: var(--danger); }
.gp-record > summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 22px; color: var(--ink); cursor: pointer; font-size: .9375rem; font-weight: 650; list-style: none; }
.gp-record > summary::-webkit-details-marker { display: none; }
.gp-record > summary .app-icon { color: var(--muted); transition: transform var(--speed); }
.gp-record[open] > summary .app-icon { transform: rotate(180deg); }
.gp-record__body { padding: 0 22px 20px; }
.gp-record__head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.gp-record__list { margin: 10px 0 0; padding: 0; list-style: none; }
.gp-record__row { display: flex; align-items: center; gap: 10px; width: 100%; padding: 9px 0; border: 0; border-top: 1px solid var(--line); background: transparent; color: var(--ink); text-align: left; cursor: pointer; font-size: .8125rem; }
.gp-record__row:hover .gp-state { color: var(--primary); }
.gp-record__row time { margin-left: auto; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.gp-enable { border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface2); }
.gp-enable > summary { padding: 11px 14px; color: var(--muted); cursor: pointer; font-size: .8125rem; font-weight: 600; }
.gp-enable p { margin: 0; padding: 0 14px 13px; color: var(--muted); font-size: .8125rem; line-height: 1.7; }
.gp-fallback { border-bottom: 0; }
.gp-dialog { width: min(520px, calc(100vw - 32px)); padding: 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); color: var(--ink); }
.gp-dialog > header { padding: 18px 22px 0; }
.gp-dialog h3 { margin: 0; font-size: 1.0625rem; }
.gp-dialog__body { padding: 10px 22px 18px; }
.gp-dialog__body p { margin: 0; color: var(--muted); font-size: .875rem; line-height: 1.75; }
.gp-dialog__body p + p { margin-top: 8px; }
.gp-dialog__body code { color: var(--ink); font-size: 12px; overflow-wrap: anywhere; }
.gp-dialog > footer { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 22px; border-top: 1px solid var(--line); background: var(--surface2); }
.gp-dialog::backdrop { background: rgba(12, 22, 45, .45); }
@media (max-width: 600px) {
  .github-publication__head, .gp-step, .gp-record > summary { padding-inline: 16px; }
  .gp-status { padding-inline: 16px; }
  .gp-record__body { padding: 0 16px 16px; }
  .gp-authorize { grid-template-columns: 1fr; }
  .gp-authorize .button { justify-self: start; }
  .gp-metrics { grid-template-columns: 1fr; }
  .gp-metrics > div + div { border-left: 0; border-top: 1px solid var(--line); }
  .gp-actions--end .button { width: 100%; }
  .gp-finding { grid-template-columns: auto minmax(0, 1fr); align-items: start; }
  .gp-finding > input { margin-top: 4px; }
  .gp-finding > .status-badge { grid-column: 2; justify-self: start; }
  .gp-items a { margin-left: 0; }
  .gp-dialog > footer { flex-wrap: wrap; }
  .gp-dialog > footer .button { flex: 1 1 140px; }
}
</style>
