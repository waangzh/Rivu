<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { getDiagnostics, getModelProviders, getProviderModels, getProviderPresets, testModelConnection, updateSystemSettings } from '../../api/client';
import type { SystemDiagnostics, ProviderInfo, ModelProfile, ProviderPreset } from '../../types/operations';
import { usePreferences } from '../../composables/usePreferences';
import { createProvider, discoveredProfile, newProfile, profileSource, protocolLabel, providerDisplayNames, providerIsCustom, usesPresetProtocol } from '../../utils/providerPresets';
import AppIcon from '../common/AppIcon.vue';
const emit = defineEmits<{ saved: [] }>();
const { theme, density } = usePreferences();
const state = ref<SystemDiagnostics | null>(null), error = ref(''), feedback = ref(''), busy = ref(false);
const retention = ref(30);
const providers = ref<ProviderInfo[]>([]), defaultProvider = ref(''), selectedProvider = ref('');
const presets = ref<ProviderPreset[]>([]), selectedPreset = ref('deepseek');
const savedDefaultProvider = ref('');
const savedConfigs = ref<Record<string, string>>({});
const catalogModels = ref<string[] | null>(null), catalogQuery = ref('');
const filteredCatalog = computed(() => (catalogModels.value || []).filter(model =>
  model.toLowerCase().includes(catalogQuery.value.trim().toLowerCase())));
watch(selectedProvider, () => { catalogModels.value = null; catalogQuery.value = ''; });
const providerNames = computed(() => providerDisplayNames(providers.value, presets.value));
const unsaved = computed(() => (id: string) => {
  const item = providers.value.find(provider => provider.id === id);
  return !item || savedConfigs.value[id] !== JSON.stringify(item.config);
});
const protocols = ['openai-chat', 'openai-responses'] as const;
const formats = ['openai', 'deepseek', 'glm', 'kimi', 'qwen', 'chat-template', 'anthropic', 'google-budget', 'google-level'] as const;
const efforts = ['minimal', 'low', 'medium', 'high', 'max', 'xhigh'] as const;
function effortsFor(format: ModelProfile['reasoning_format']) {
  if (format === 'deepseek') return ['low', 'high', 'max'];
  if (format === 'google-level') return ['minimal', 'low', 'medium', 'high'];
  if (format === 'anthropic') return ['low', 'medium', 'high'];
  if (format === 'openai') return efforts;
  return [];
}
function addProvider() {
  const preset = presets.value.find(item => item.id === selectedPreset.value);
  const item = createProvider(preset, providers.value);
  providers.value.push(item);
  selectedProvider.value = item.id;
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
async function findModels(item: ProviderInfo) {
  busy.value = true; error.value = ''; feedback.value = ''; catalogModels.value = null;
  try {
    const result = await getProviderModels(item.id);
    if (!disposed) catalogModels.value = result.models.map(model => model.id);
  } catch (cause) { if (!disposed) error.value = cause instanceof Error ? cause.message : '获取模型列表失败'; }
  finally { if (!disposed) busy.value = false; }
}
function addDiscoveredModel(item: ProviderInfo, modelId: string) {
  item.config.models.push(discoveredProfile(item, modelId, presets.value));
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
    const [value, registry, availablePresets] = await Promise.all([getDiagnostics(), getModelProviders(), getProviderPresets()]); if (disposed) return;
    providers.value = registry.providers; defaultProvider.value = registry.default_provider;
    savedDefaultProvider.value = registry.default_provider;
    presets.value = availablePresets;
    savedConfigs.value = Object.fromEntries(registry.providers.map(item => [item.id, JSON.stringify(item.config)]));
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
    if (!disposed) {
      const registry = await getModelProviders();
      if (disposed) return;
      providers.value = registry.providers;
      savedDefaultProvider.value = registry.default_provider;
      savedConfigs.value = Object.fromEntries(registry.providers.map(item => [item.id, JSON.stringify(item.config)]));
      state.value = value; feedback.value = '设置已保存，新的审查将使用更新后的配置。'; emit('saved');
    }
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
        <div class="settings-provider-heading"><h3>已配置供应商</h3></div>
        <div class="settings-provider-list" role="group" aria-label="已配置供应商">
          <button v-for="item in providers" :key="item.id" type="button" class="settings-provider"
            :class="{ 'is-selected': selectedProvider === item.id }" :aria-pressed="selectedProvider === item.id"
            :disabled="busy" @click="selectedProvider = item.id">
            <span class="settings-provider-name">{{ providerNames[item.id] }}</span>
            <span v-if="defaultProvider === item.id" class="settings-provider-default">{{ savedDefaultProvider === item.id ? '默认' : '默认待保存' }}</span>
            <span class="settings-provider-credential">{{ unsaved(item.id) ? '未保存' : item.credentials_configured ? '凭据已配置' : '缺少凭据' }}</span>
          </button>
        </div>
        <div class="grid two"><label>添加供应商<select v-model="selectedPreset" :disabled="busy"><option v-for="preset in presets" :key="preset.id" :value="preset.id">{{ preset.label }}</option><option value="custom">自定义 OpenAI 兼容服务</option></select></label><button type="button" class="btn secondary" :disabled="busy" @click="addProvider">添加供应商</button></div>
        <template v-for="item in providers" :key="item.id"><div v-if="item.id === selectedProvider" class="section-stack">
          <div class="settings-provider-detail">
            <h3>{{ providerNames[item.id] }}</h3>
            <div class="settings-model-actions">
              <button v-if="defaultProvider !== item.id" type="button" class="btn secondary" :disabled="busy" @click="defaultProvider = item.id">设为默认</button>
              <button type="button" class="btn secondary" :disabled="busy || providers.length <= 1" @click="removeProvider(item.id)">删除供应商</button>
            </div>
          </div>
          <p v-if="!item.credentials_configured" class="field-hint">缺少凭据：请在服务端环境或 <code>backend/.env</code> 中设置 <code>{{ item.config.api_key_env }}</code>。</p>
          <p v-else class="field-hint">服务端凭据已配置。</p>
          <label>默认模型<select v-model="item.config.default_model" required :disabled="busy"><option v-for="profile in item.config.models" :key="profile.model_id" :value="profile.model_id">{{ profile.model_id || '请填写模型名称' }}</option></select></label>
          <details><summary>连接高级设置</summary><div class="section-stack space-top"><label>接口地址<input v-model="item.config.base_url" type="url" required :disabled="busy" /></label><label v-if="providerIsCustom(item, presets)">服务端密钥变量<input v-model="item.config.api_key_env" required :disabled="busy" /></label><p class="field-hint">更改已冻结任务的接口地址会影响其恢复。</p></div></details>
          <div v-if="['openai', 'deepseek', 'openai-compatible'].includes(item.config.provider)" class="section-stack">
            <div><button type="button" class="btn secondary" :disabled="busy || unsaved(item.id)" :title="unsaved(item.id) ? '请先保存更改' : '从供应商获取模型列表'" @click="findModels(item)">查找供应商模型</button></div>
            <p v-if="unsaved(item.id)" class="field-hint">请先保存供应商配置，再查询模型。</p>
            <template v-if="catalogModels">
              <label>搜索模型<input v-model="catalogQuery" type="search" placeholder="输入模型名称" /></label>
              <div v-if="filteredCatalog.length" class="settings-catalog" aria-label="供应商模型列表">
                <div v-for="modelId in filteredCatalog" :key="modelId" class="settings-catalog-row">
                  <span>{{ modelId }}</span>
                  <button type="button" class="btn secondary" :disabled="busy || item.config.models.some(profile => profile.model_id === modelId)" @click="addDiscoveredModel(item, modelId)">{{ item.config.models.some(profile => profile.model_id === modelId) ? '已添加' : '添加' }}</button>
                </div>
              </div>
              <p v-else class="field-hint">没有匹配的模型。</p>
              <p class="field-hint">模型列表不代表审查兼容性；非预设模型需要确认原生工具调用能力。</p>
            </template>
          </div>
          <section v-for="(profile, index) in item.config.models" :key="index" class="settings-model-profile section-stack">
            <div class="card-header"><h3>模型配置</h3><div class="settings-model-actions"><button type="button" class="btn secondary" :disabled="busy || !profile.model_id || unsaved(item.id)" :title="unsaved(item.id) ? '请先保存更改' : '测试模型工具调用，会产生用量'" @click="testConnection(item.id, profile.model_id)">测试连接</button><button type="button" class="btn secondary" :disabled="busy || item.config.models.length <= 1" @click="removeProfile(item, index)">删除</button></div></div>
            <label>模型名称<input v-model="profile.model_id" required :disabled="busy" /></label>
            <p class="field-hint">模型能力：{{ profileSource(item, profile, presets) }}。{{ profile.supports_tools ? '支持原生工具调用' : '尚未确认支持原生工具调用' }}</p>
            <label v-if="profile.reasoning_format"><input v-model="profile.thinking" type="checkbox" :disabled="busy || profile.reasoning_format === 'openai'" />默认开启 Thinking</label>
            <p v-if="profile.reasoning_format === 'kimi' && profile.thinking" class="field-hint">Kimi 官方建议 Thinking 工具调用的输出上限至少为 16000 Token；实际审查使用服务端配置的上限。</p>
            <details><summary>模型高级设置</summary><div class="section-stack space-top">
            <p v-if="usesPresetProtocol(item, profile, presets)" class="field-hint">接口协议：{{ protocolLabel(profile.api_protocol) }}</p>
            <label v-else>接口协议<select v-model="profile.api_protocol" :disabled="busy"><option v-if="profile.api_protocol === 'anthropic' || profile.api_protocol === 'google'" :value="profile.api_protocol">{{ protocolLabel(profile.api_protocol) }}</option><option v-for="protocol in protocols" :key="protocol" :value="protocol">{{ protocolLabel(protocol) }}</option></select></label>
            <div class="grid two"><label>上下文 Token<input v-model.number="profile.context_tokens" type="number" min="8192" max="1000000" required :disabled="busy" /></label><label>最大输出 Token<input v-model.number="profile.max_output_tokens" type="number" min="256" max="125000" required :disabled="busy" /></label></div>
            <p class="field-hint">审查使用的上下文与输出额度同时受服务端资源策略限制。</p>
            <div class="grid two"><label><input v-model="profile.supports_tools" type="checkbox" :disabled="busy" />原生 Tool Calling</label><label><input v-model="profile.supports_temperature" type="checkbox" :disabled="busy" />允许 temperature</label></div>
            <div v-if="profile.api_protocol === 'openai-chat'" class="grid two"><label>输出上限参数<select v-model="profile.max_tokens_parameter" :disabled="busy"><option>max_tokens</option><option>max_completion_tokens</option></select></label><label><input v-model="profile.stream_usage" type="checkbox" :disabled="busy" />支持流式用量</label></div>
            <label>推理参数格式<select v-model="profile.reasoning_format" :disabled="busy"><option :value="null">不支持推理</option><option v-for="format in formats" :key="format">{{ format }}</option></select></label>
            <template v-if="profile.reasoning_format">
              <div role="group" aria-label="支持的推理强度" class="grid two"><label v-for="effort in effortsFor(profile.reasoning_format)" :key="effort"><input v-model="profile.reasoning_efforts" type="checkbox" :value="effort" :disabled="busy" />{{ effort }}</label></div>
              <label>默认推理强度<select v-model="profile.reasoning_effort" :disabled="busy"><option :value="null">不指定</option><option v-for="effort in profile.reasoning_efforts" :key="effort">{{ effort }}</option></select></label>
              <label v-if="['anthropic','google-budget'].includes(profile.reasoning_format)">推理 Token 预算<input v-model.number="profile.reasoning_budget_tokens" type="number" min="1024" required :disabled="busy" /></label>
            </template>
            </div></details>
          </section>
          <button type="button" class="btn secondary" :disabled="busy" @click="item.config.models.push(newProfile())">添加自定义模型</button>
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
