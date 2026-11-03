import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const source = { kind: 'local_git', repository_key: 'project', base_ref: 'main', head_ref: 'feature', input_id: 'a'.repeat(32) };
const options = { depth: 'standard', paths: null, focus: ['correctness'], limits: {}, include_pr_discussions: false };

test('本地创建请求与恢复保留固定输入，拒绝混用 GitHub 能力', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  const previousStorage = globalThis.localStorage, previousFetch = globalThis.fetch;
  const values = new Map();
  globalThis.localStorage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  try {
    const { readPendingCreation, savePendingCreation } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    const pending = { key: 'local-key', startedAt: new Date().toISOString(), input: { source, ...options } };
    savePendingCreation(pending);
    assert.deepEqual(readPendingCreation(), pending);
    let submitted;
    globalThis.fetch = async (_url, init) => {
      submitted = JSON.parse(init.body);
      assert.equal(init.headers['Idempotency-Key'], pending.key);
      return new Response(JSON.stringify({ id: 'local-review' }), { status: 202 });
    };
    await createReview(pending.input, undefined, pending.key);
    assert.deepEqual(submitted.source, source);
    assert.equal(submitted.pr_url, undefined);
    for (const invalid of [{ auto_publish: true }, { include_pr_discussions: true }, { pr_url: 'https://github.com/o/r/pull/1' }]) {
      savePendingCreation({ ...pending, input: { ...pending.input, ...invalid } });
      assert.equal(readPendingCreation(), null);
    }
  } finally { globalThis.localStorage = previousStorage; globalThis.fetch = previousFetch; await server.close(); }
});

test('本地来源展示提交选择、快照边界并隐藏自动发布', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/NewReviewPage.vue');
    const html = await renderToString(createSSRApp(Page, {
      prUrl: '', localSource: source, model: '', models: [], options, busy: false,
      capabilities: { local_repositories: [{ key: 'project', label: 'project' }], limits: { defaults: {}, maxima: {} } },
    }));
    assert.match(html, /本地 Git/);
    assert.match(html, /固定并预览快照/);
    assert.match(html, /未跟踪文件不会纳入/);
    assert.doesNotMatch(html, /发布操作口令/);
  } finally { await server.close(); }
});

test('本地工作台不依赖 PR 信息且不提供 GitHub 发布面板', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { default: Workspace } = await server.ssrLoadModule('/src/components/review/ReviewWorkspace.vue');
    const task = {
      id: 'local', status: 'completed', phase: 'finished', pr: null, pr_url: null,
      source: { ...source, title: '<local project>', input_digest: 'd'.repeat(64) },
      snapshot: { repository: 'local:project', base_sha: 'b'.repeat(40), declared_base_sha: 'b'.repeat(40), head_sha: 'c'.repeat(40) },
      configuration: { model: 'fixture', options }, changed_files: [],
      plan: { units: [], excluded: {}, history: [] }, report: null,
    };
    const html = await renderToString(createSSRApp(Workspace, { task, section: 'publish' }));
    assert.match(html, /&lt;local project&gt;/);
    assert.match(html, /本地固定快照/);
    assert.match(html, /重新审查此快照/);
    assert.doesNotMatch(html, /发布到 GitHub|查看自动发布|<local project>/);
  } finally { await server.close(); }
});
