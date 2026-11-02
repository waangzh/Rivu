import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const input = {
  pr_url: 'https://github.com/o/r/pull/1', depth: 'standard', paths: null,
  focus: ['correctness'], limits: {}, auto_publish: true,
};

async function withServer(operation) {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom',
  });
  try { await operation(server); } finally { await server.close(); }
}

test('自动发布授权仅在请求头中发送，网络重试沿用授权与幂等标识', async () => {
  await withServer(async server => {
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    const previous = globalThis.fetch, calls = [];
    globalThis.fetch = async (url, init) => {
      calls.push({ url, init });
      return calls.length === 1 ? Response.json({}, { status: 503 }) : Response.json({ id: 'run' });
    };
    try {
      assert.equal((await createReview(input, undefined, 'stable-key', 'operator-fixture')).id, 'run');
      assert.equal(calls.length, 2);
      for (const { init } of calls) {
        assert.equal(init.headers.Authorization, 'Bearer operator-fixture');
        assert.equal(init.headers['Idempotency-Key'], 'stable-key');
        assert.equal(JSON.parse(init.body).auto_publish, true);
        assert.ok(!init.body.includes('operator-fixture'));
      }
      calls.length = 0;
      await createReview({ ...input, auto_publish: false }, undefined, 'plain', 'operator-fixture');
      assert.equal(calls[0].init.headers.Authorization, undefined);
    } finally { globalThis.fetch = previous; }
  });
});

test('无效操作口令不会自动重试，自动发布状态读取不携带口令', async () => {
  await withServer(async server => {
    const { createReview, getAutomaticPublication, ApiError } = await server.ssrLoadModule('/src/api/client.ts');
    const previous = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      return Response.json({ detail: { code: 'publication_operator_unauthorized' } }, { status: 403 });
    };
    try {
      await assert.rejects(createReview(input, undefined, 'key', 'wrong'),
        error => error instanceof ApiError && error.status === 403);
      assert.equal(calls, 1);
      globalThis.fetch = async (url, init) => {
        assert.equal(url, '/api/reviews/run/automatic-publication');
        assert.equal(init.headers.Authorization, undefined);
        return Response.json({ state: 'uncertain', publication_id: 'p', last_error_code: 'batch_result_unknown' });
      };
      assert.equal((await getAutomaticPublication('run')).state, 'uncertain');
    } finally { globalThis.fetch = previous; }
  });
});

test('刷新恢复保留自动发布选择，非法选择被拒绝，页面要求重新输入操作口令', async () => {
  await withServer(async server => {
    const { savePendingCreation, readPendingCreation } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewSubmissionPage.vue');
    const previous = globalThis.localStorage, values = new Map();
    globalThis.localStorage = {
      setItem: (key, value) => values.set(key, value),
      getItem: key => values.get(key) ?? null,
      removeItem: key => values.delete(key),
    };
    try {
      const pending = { key: 'same-request', input, startedAt: new Date().toISOString() };
      savePendingCreation(pending);
      assert.equal(readPendingCreation().input.auto_publish, true);
      assert.ok(!values.get('rg-review-creation').includes('operator-fixture'));
      const html = await renderToString(createSSRApp(Component, {
        creation: pending, submitting: false, rejected: false, error: '', publicationToken: '',
      }));
      assert.match(html, /重新输入发布操作口令/);
      assert.match(html, /type="password"/);
      assert.match(html, /<button[^>]+disabled[^>]*>继续确认/);
      savePendingCreation({ ...pending, input: { ...input, auto_publish: 'true' } });
      assert.equal(readPendingCreation(), null);
    } finally {
      if (previous === undefined) delete globalThis.localStorage;
      else globalThis.localStorage = previous;
    }
  });
});

test('自动发布状态区分等待、人工确认、未知与成功', async () => {
  await withServer(async server => {
    const { automaticPublicationStateLabel, publicationErrorText } =
      await server.ssrLoadModule('/src/utils/publicationPresentation.ts');
    assert.match(automaticPublicationStateLabel.waiting_review, /审查完成后/);
    assert.match(automaticPublicationStateLabel.manual_required, /人工确认/);
    assert.match(automaticPublicationStateLabel.uncertain, /无法确认/);
    assert.match(automaticPublicationStateLabel.succeeded, /已发布/);
    assert.match(publicationErrorText('automatic_authorization_expired'), /授权已过期/);
    assert.match(publicationErrorText('review_partial'), /不完整/);
    assert.equal(publicationErrorText('无法获取固定提交', '无法获取固定提交'), '无法获取固定提交');
  });
});
