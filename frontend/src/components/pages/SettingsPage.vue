<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { getDiagnostics, getModelProviders, testModelConnection, updateSystemSettings } from '../../api/client';
import type { SystemDiagnostics, ProviderInfo, ModelProfile } from '../../types/operations';
import { usePreferences } from '../../composables/usePreferences';
import AppIcon from '../common/AppIcon.vue';
const emit = defineEmits<{ saved: [] }>();
const { theme, density } = usePreferences();
const state = ref<SystemDiagnostics | null>(null), error = ref(''), feedback = ref(''), busy = ref(false);
const retention = ref(30);
const providers = ref<ProviderInfo[]>([]), defaultProvider = ref(''), selectedProvider = ref('');
const newProviderId = ref('');
const protocols = ['openai-chat', 'openai-responses', 'anthropic', 'google'] as const;
const formats = ['openai', 'deepseek', 'qwen', 'chat-template', 'anthropic', 'google-budget', 'google-level'] as const;
const efforts = ['minimal', 'low', 'medium', 'high', 'max', 'xhigh'] as const;
function effortsFor(format: ModelProfile['reasoning_format']) {
  if (format === 'deepseek') return ['low', 'high', 'max'];
  if (format === 'google-level') return ['minimal', 'low', 'medium', 'high'];
  if (format === 'anthropic') return ['low', 'medium', 'high'];
  if (format === 'openai') return efforts;
  return [];
}
function newProfile(): ModelProfile {
  return { model_id: '', api_protocol: 'openai-chat', supports_tools: true,
    reasoning_format: null, reasoning_efforts: [], thinking: false, reasoning_effort: null,
    reasoning_budget_tokens: 2048, supports_temperature: true, max_tokens_parameter: 'max_tokens',
    stream_usage: false, context_tokens: 128000, max_output_tokens: 4096 };
}
function addProvider() {
  const id = newProviderId.value.trim();
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(id) || providers.value.some(item => item.id === id)) {
    error.value = 'Provider ID 使用字母、数字、下划线或短横线，且不能重复。'; return;
  }
  providers.value.push({ id, credentials_configured: false, config: { provider: 'openai-compatible',
    base_url: 'http://localhost:8000/v1', api_key_env: 'MODEL_API_KEY', default_model: '', models: [newProfile()] } });
  selectedProvider.value = id; newProviderId.value = '';
}
function removeProvider(id: string) {
  if (providers.value.length <= 1) return;
  providers.value = providers.value.filter(item => item.id !== id);
  if (defaultProvider.value === id) defaultProvider.value = providers.value[0].id;
  selectedProvider.value = defaultProvider.value;
}
function removeProfile(provider: ProviderInfo, index: number) {
  if (provider.config.models.length <= 1) return;
  const [removed] = provider.config.models.splice(index, 1);
  if (provider.config.default_model === removed.model_id) {
    provider.config.default_model = provider.config.models[0].model_id;
  }
}
async function testConnection(id: string, modelId: string) {
  busy.value = true; error.value = ''; feedback.value = '';
  try {
    const result = await testModelConnection(id, modelId);
    feedback.value = result.status === 'connected' ? '连接成功，已收到原生工具调用。' : '连接成功，但未收到要求的工具调用。';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '连接测试失败'; }
  finally { if (!disposed) busy.value = false; }
}
const tracingLabels = {
  disabled: '未启用', missing_credentials: '缺少凭据', pending: '等待首次上报',
  active: '最近上报成功', degraded: '存在上报缺失',
};
let disposed = false;
async function load() {
  busy.value = true; error.value = '';
  try {
    const [value, registry] = await Promise.all([getDiagnostics(), getModelProviders()]); if (disposed) return;
    providers.value = registry.providers; defaultProvider.value = registry.default_provider;
    selectedProvider.value = registry.default_provider;
    state.value = value; retention.value = value.retention_days;
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '读取设置失败'; }
  finally { if (!disposed) busy.value = false; }
}
async function save() {
  if (busy.value) return;
  busy.value = true; error.value = ''; feedback.value = '';
  try {
    const value = await updateSystemSettings({ model_providers: Object.fromEntries(providers.value.map(item => [item.id, item.config])),
      default_provider: defaultProvider.value, retention_days: retention.value });
    if (!disposed) { state.value = value; feedback.value = '设置已保存，新的审查将使用更新后的配置。'; emit('saved'); }
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '保存设置失败'; }
  finally { if (!disposed) busy.value = false; }
}
onMounted(load);
onBeforeUnmount(() => { disposed = true; });
</script>
<template>
  <div class="page-head"><div><h1>系统设置</h1><p class="title-sub">管理模型连接、记录保留和本机显示偏好。</p></div><button class="btn secondary" :disabled="busy" @click="load">刷新诊断</button></div>
  <p v-if="error" class="notice warn space-bottom" role="alert">{{ error }}</p><p v-if="feedback" class="notice space-bottom" role="status">{{ feedback }}</p>
  <div class="split-70"><div class="section-stack">
    <section class="card"><div class="card-pad"><div class="card-header"><div><h2>模型与记录</h2><p>API Key 由服务端配置，不在前端展示。</p></div><AppIcon name="server" /></div></div>
      <form @submit.prevent="save"><div class="settings-form">
        <label>默认 Provider<select v-model="defaultProvider" :disabled="busy"><option v-for="item in providers" :key="item.id" :value="item.id">{{ item.id }}</option></select></label>
        <label>编辑 Provider<select v-model="selectedProvider" :disabled="busy"><option v-for="item in providers" :key="item.id" :value="item.id">{{ item.id }}</option></select></label>
        <div class="grid two"><label>新增 Provider ID<input v-model="newProviderId" :disabled="busy" placeholder="例如 local-vllm" /></label><button type="button" class="btn secondary" :disabled="busy" @click="addProvider">添加 Provider</button></div>
        <template v-for="item in providers" :key="item.id"><div v-if="item.id === selectedProvider" class="section-stack">
          <div class="settings-actions"><span></span><button type="button" class="btn secondary" :disabled="busy || providers.length <= 1" @click="removeProvider(item.id)">删除 Provider</button></div>
          <div class="grid two"><label>Provider 类型<select v-model="item.config.provider" :disabled="busy"><option v-for="kind in ['openai','deepseek','openai-compatible','anthropic','google']" :key="kind">{{ kind }}</option></select></label><label>API Key 环境变量名<input v-model="item.config.api_key_env" required :disabled="busy" /></label></div>
          <p class="field-hint">凭据{{ item.credentials_configured ? '已配置' : '尚未配置' }}；只填写服务端环境变量名，不填写 Key。修改后先保存，再测试连接。</p>
          <label>模型接口<input v-model="item.config.base_url" type="url" required :disabled="busy" /></label>
          <label>默认模型<input v-model="item.config.default_model" required :disabled="busy" /></label>
          <section v-for="(profile, index) in item.config.models" :key="index" class="settings-model-profile section-stack">
            <div class="card-header"><h3>模型 {{ index + 1 }}</h3><div class="settings-model-actions"><button type="button" class="btn secondary" :disabled="busy || !profile.model_id" @click="testConnection(item.id, profile.model_id)">测试连接（产生模型用量）</button><button type="button" class="btn secondary" :disabled="busy || item.config.models.length <= 1" @click="removeProfile(item, index)">删除</button></div></div>
            <div class="grid two"><label>模型 ID<input v-model="profile.model_id" required :disabled="busy" /></label><label>API 协议<select v-model="profile.api_protocol" :disabled="busy"><option v-for="protocol in protocols" :key="protocol">{{ protocol }}</option></select></label></div>
            <div class="grid two"><label>上下文 Token<input v-model.number="profile.context_tokens" type="number" min="8192" max="1000000" required :disabled="busy" /></label><label>最大输出 Token<input v-model.number="profile.max_output_tokens" type="number" min="256" max="125000" required :disabled="busy" /></label></div>
            <div class="grid two"><label><input v-model="profile.supports_tools" type="checkbox" :disabled="busy" />原生 Tool Calling</label><label><input v-model="profile.supports_temperature" type="checkbox" :disabled="busy" />允许 temperature</label></div>
            <div v-if="profile.api_protocol === 'openai-chat'" class="grid two"><label>输出上限参数<select v-model="profile.max_tokens_parameter" :disabled="busy"><option>max_tokens</option><option>max_completion_tokens</option></select></label><label><input v-model="profile.stream_usage" type="checkbox" :disabled="busy" />支持流式用量</label></div>
            <label>推理参数格式<select v-model="profile.reasoning_format" :disabled="busy"><option :value="null">不支持推理</option><option v-for="format in formats" :key="format">{{ format }}</option></select></label>
            <template v-if="profile.reasoning_format">
              <label><input v-model="profile.thinking" type="checkbox" :disabled="busy" />默认开启 Thinking</label>
              <div role="group" aria-label="支持的推理强度" class="grid two"><label v-for="effort in effortsFor(profile.reasoning_format)" :key="effort"><input v-model="profile.reasoning_efforts" type="checkbox" :value="effort" :disabled="busy" />{{ effort }}</label></div>
              <label>默认推理强度<select v-model="profile.reasoning_effort" :disabled="busy"><option :value="null">不指定</option><option v-for="effort in profile.reasoning_efforts" :key="effort">{{ effort }}</option></select></label>
              <label v-if="['anthropic','google-budget'].includes(profile.reasoning_format)">推理 Token 预算<input v-model.number="profile.reasoning_budget_tokens" type="number" min="1024" required :disabled="busy" /></label>
            </template>
          </section>
          <button type="button" class="btn secondary" :disabled="busy" @click="item.config.models.push(newProfile())">添加模型</button>
        </div></template>
        <label>历史保留天数<input v-model.number="retention" type="number" min="1" max="3650" required :disabled="busy" /></label>
        <p class="field-hint">模型须支持原生工具调用与结构化提交。更新设置不会修改已经冻结的任务配置；保留运行中任务所用的 Provider ID、类型和端点。</p>
      </div><div class="settings-actions"><p class="muted small">更改保存在本机服务端。</p><button class="btn primary" :disabled="busy || !state">{{ busy ? '处理中…' : '保存设置' }}</button></div></form>
    </section>
    <section class="card card-pad"><div class="card-header"><h2>外观偏好</h2><AppIcon name="settings" /></div><div class="grid two"><label class="field-label">主题<select v-model="theme" class="select"><option value="light">浅色</option><option value="dark">深色</option></select></label><label class="field-label">信息密度<select v-model="density" class="select"><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></label></div><p class="field-hint">仅保存在当前浏览器，不改变审查结果或运行保护上限。</p></section>
  </div><aside class="plan-aside"><section class="card card-pad"><div class="card-header"><h2>服务状态</h2><AppIcon name="shield" /></div><p v-if="!state" class="muted" role="status">{{ busy ? '正在读取诊断…' : '诊断尚不可用' }}</p>
    <template v-else><div v-for="item in [['数据库',state.database_schema_current?'当前结构':'需要执行迁移'],['审查 Worker',state.worker_status==='running'?'运行中':'空闲'],['模型凭据',state.configured_secrets.llm_provider?'已配置':'未配置'],['证据目录',state.artifact_directory_writable?'可写':'不可写'],['追踪上传',tracingLabels[state.langsmith.status]]]" :key="item[0]" class="aside-stat"><span class="muted">{{ item[0] }}</span><strong>{{ item[1] }}</strong></div>
      <p v-if="state.langsmith_enabled" class="muted small space-top">{{ state.langsmith.capture_content ? '追踪包含经脱敏、截断的模型消息和工具正文。' : '追踪仅上传结构化元数据。' }}<template v-if="state.langsmith.last_success_at">最近上报：{{ new Date(state.langsmith.last_success_at).toLocaleString() }}。</template><template v-if="state.langsmith.dropped_operations">缺失 {{ state.langsmith.dropped_operations }} 次上报操作，轨迹可能不完整。</template></p>
    </template>
    <p class="notice small space-top">Rivu 不执行目标仓库代码、测试或构建。发布权限与只读审查凭据分别配置。</p>
  </section></aside></div>
</template>
