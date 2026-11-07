import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { reactive } from 'vue';

const profile = { model_id: 'reasoning-model', api_protocol: 'openai-responses',
  supports_tools: true, reasoning_format: 'openai', thinking: true,
  reasoning_effort: 'medium', reasoning_efforts: ['low', 'medium', 'high'],
  reasoning_budget_tokens: 2048, supports_temperature: false, context_tokens: 128000,
  max_output_tokens: 8192, max_tokens_parameter: 'max_completion_tokens', stream_usage: true };

test('任务创建与重试保留 Provider、Thinking 和推理强度', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  const previousFetch = globalThis.fetch, previousStorage = globalThis.localStorage;
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  try {
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    const { savePendingCreation, readPendingCreation } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const input = { pr_url: 'https://github.com/o/r/pull/1', depth: 'standard', paths: null,
      focus: ['correctness'], limits: {}, provider_id: 'cloud', model: 'reasoning-model',
      thinking: false, reasoning_effort: 'medium' };
    const pending = { key: 'model-selection', input, startedAt: new Date().toISOString() };
    savePendingCreation(pending);
    assert.deepEqual(readPendingCreation(), pending);
    let body;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(init.body);
      assert.equal(init.headers['Idempotency-Key'], pending.key);
      return new Response(JSON.stringify({ id: 'review' }), { status: 202 });
    };
    await createReview(input, undefined, pending.key);
    assert.deepEqual(body, input);
  } finally { globalThis.fetch = previousFetch; globalThis.localStorage = previousStorage; await server.close(); }
});

test('新建页显示 Provider、真实能力与模型推理强度', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/NewReviewPage.vue');
    const html = await renderToString(createSSRApp(Page, { prUrl: '', model: 'reasoning-model',
      models: ['reasoning-model'], providerId: 'default', providerNames: { default: 'OpenAI' }, providers: [{ id: 'default',
        credentials_configured: true, config: { provider: 'openai', default_model: 'reasoning-model', models: [profile] } }],
      busy: false, capabilities: null,
      options: { depth: 'standard', focus: ['correctness'], limits: {}, paths: null } }));
    assert.match(html, /供应商/);
    assert.match(html, /OpenAI/);
    assert.doesNotMatch(html, />default</);
    assert.match(html, /openai-responses/);
    assert.match(html, /128,000/);
    assert.match(html, /8,192/);
    assert.match(html, /Thinking/);
    assert.match(html, /推理强度/);
    assert.match(html, /medium/);
  } finally { await server.close(); }
});

test('供应商名称根据配置展示并区分同名服务', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { providerDisplayNames, providerIsCustom } = await server.ssrLoadModule('/src/utils/providerPresets.ts');
    const deepseek = { id: 'deepseek', label: 'DeepSeek', config: { base_url: 'https://api.deepseek.com' } };
    const openai = { id: 'openai', label: 'OpenAI', config: { base_url: 'https://api.openai.com/v1' } };
    const providers = [
      { id: 'default', config: { provider: 'deepseek', base_url: 'https://api.deepseek.com/v1' } },
      { id: 'another', config: { provider: 'openai-compatible', base_url: 'https://api.deepseek.com/' } },
      { id: 'openai-account', config: { provider: 'openai', base_url: 'https://api.openai.com/v1' } },
      { id: 'custom', config: { provider: 'openai-compatible', base_url: 'https://example.com/v1' } },
    ];
    assert.deepEqual(providerDisplayNames(providers, [deepseek, openai]), {
      default: 'DeepSeek 1', another: 'DeepSeek 2', 'openai-account': 'OpenAI', custom: '自定义服务',
    });
    assert.equal(providerIsCustom(providers[1], [deepseek, openai]), false);
    assert.equal(providerIsCustom(providers[3], [deepseek, openai]), true);
    assert.equal(providerDisplayNames([providers[0]], []).default, 'DeepSeek');
  } finally { await server.close(); }
});

test('选择官方供应商会复制模型预设，自定义模型的工具能力默认未知', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { createProvider, discoveredProfile, profileSource, protocolLabel, usesPresetProtocol } = await server.ssrLoadModule('/src/utils/providerPresets.ts');
    const preset = reactive({ id: 'deepseek', label: 'DeepSeek', config: {
      provider: 'deepseek', base_url: 'https://api.deepseek.com', api_key_env: 'DEEPSEEK_API_KEY',
      default_model: profile.model_id, models: [profile] } });
    const first = createProvider(preset, []);
    assert.equal(first.id, 'deepseek');
    assert.equal(first.config.provider, 'deepseek');
    assert.equal(profileSource(first, first.config.models[0], [preset]), '内置预设');
    assert.equal(usesPresetProtocol(first, first.config.models[0], [preset]), true);
    assert.equal(protocolLabel(first.config.models[0].api_protocol), 'Responses');
    assert.equal(discoveredProfile(first, profile.model_id, [preset]).supports_tools, true);
    assert.equal(discoveredProfile(first, 'unlisted-model', [preset]).supports_tools, false);
    const second = createProvider(preset, [first]);
    assert.equal(second.id, 'deepseek-2');
    first.config.models[0].supports_tools = false;
    assert.equal(profileSource(first, first.config.models[0], [preset]), '手动配置');
    assert.equal(usesPresetProtocol(first, first.config.models[0], [preset]), true);
    first.config.models[0].api_protocol = 'openai-chat';
    assert.equal(usesPresetProtocol(first, first.config.models[0], [preset]), false);
    assert.equal(second.config.models[0].supports_tools, true);
    const custom = createProvider(undefined, []);
    assert.equal(custom.config.provider, 'openai-compatible');
    assert.equal(custom.config.models[0].supports_tools, false);
    assert.equal(usesPresetProtocol(custom, custom.config.models[0], [preset]), false);
    assert.equal(protocolLabel(custom.config.models[0].api_protocol), 'Chat Completions');
  } finally { await server.close(); }
});
