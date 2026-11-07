import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

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
      models: ['reasoning-model'], providerId: 'cloud', providers: [{ id: 'cloud',
        credentials_configured: true, config: { provider: 'openai', default_model: 'reasoning-model', models: [profile] } }],
      busy: false, capabilities: null,
      options: { depth: 'standard', focus: ['correctness'], limits: {}, paths: null } }));
    assert.match(html, /Provider/);
    assert.match(html, /openai-responses/);
    assert.match(html, /128,000/);
    assert.match(html, /8,192/);
    assert.match(html, /Thinking/);
    assert.match(html, /推理强度/);
    assert.match(html, /medium/);
  } finally { await server.close(); }
});
