import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const run = (status = 'running') => ({
  id: 'progress', status, phase: 'review', created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:01:00Z', completed_at: status === 'running' ? null : '2026-11-01T00:03:00Z',
  changed_files: [], configuration: { model: 'fixture' },
  plan: { units: [{ id: 'u1', label: '<script>Auth</script>', paths: ['auth.py'] }, { id: 'u2', label: 'Storage', paths: ['store.py'] }], grouping_status: 'model' },
});
const work = (id, unit_id, kind, status) => ({
  id, unit_id, kind, status, attempt: 1, tool_calls: 2, material_delivery: 'complete',
  missing_material_reasons: {}, error_code: null, candidate_id: kind === 'verification' ? 'pending' : null,
});
const units = () => [
  work('z', 'u1', 'review', 'completed'),
  work('a', 'u2', 'review', 'running'),
  work('v', 'u1', 'verification', 'running'),
];

async function withServer(fn) {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try { await fn(server); } finally { await server.close(); }
}

test('运行记录进入进度页，终态进入概览；审查单元计数不混入核验工作', async () => {
  await withServer(async server => {
    const { initialReviewSection, progressSummary, formatElapsed, progressHeading, reviewRecordMissing } = await server.ssrLoadModule('/src/utils/reviewProgress.ts');
    const { ApiError } = await server.ssrLoadModule('/src/api/client.ts');
    assert.equal(reviewRecordMissing(new ApiError('missing', 404)), true);
    assert.equal(reviewRecordMissing(new ApiError('busy', 503)), false);
    assert.equal(reviewRecordMissing(new TypeError('network interrupted')), false);
    for (const status of ['queued', 'running']) assert.equal(initialReviewSection(status), 'activity');
    for (const status of ['completed', 'partial', 'failed', 'cancelled']) assert.equal(initialReviewSection(status), 'overview');
    const task = run();
    const summary = progressSummary(task, units(), [{ verification: null }, { verification: { verdict: 'insufficient' } }]);
    assert.deepEqual(summary, { total: 2, completed: 1, unavailable: 0, reviewing: 1, verifying: 1, pending: 1, insufficient: 1, unresolved: 0, supplementing: 0 });
    task.plan.grouping_status = 'pending';
    assert.equal(progressSummary(task, units(), []).total, null);
    assert.equal(formatElapsed('2026-11-01T00:00:00Z', '2026-11-01T00:02:18Z'), '2 分 18 秒');
    assert.equal(formatElapsed('', Date.now()), '—');
    assert.equal(progressHeading({ ...task, phase: 'overview' }), '正在整理审查结果');
    assert.equal(progressHeading(run('partial')), '审查已结束，部分审查单元未完成');
  });
});

test('进度页优先显示审查单元和核验，未加载状态不冒充零，技术用量折叠', async () => {
  await withServer(async server => {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
    const render = props => renderToString(createSSRApp(Page, { task: run(), units: units(), report: null, findingsCount: 0, ...props }));
    const html = await render({
      candidates: [{ id: 'pending', proposal: { claim: '待核验问题' }, verification: null }], syncedAt: Date.now(),
      usage: { model_calls: 1, input_tokens: null, output_tokens: null, unknown_calls: 1 },
    });
    assert.match(html, /正在分析代码与核验问题/);
    assert.match(html, /已完成 <strong>1<\/strong><span> \/ 2 个审查单元/);
    assert.match(html, /暂未确认问题/);
    assert.match(html, /不代表整体完成比例或剩余时间/);
    assert.match(html, /auth\.py/);
    assert.match(html, /&lt;script&gt;Auth&lt;\/script&gt;/);
    assert.doesNotMatch(html, /<script>/);
    assert.match(html, /<details[^>]*><summary>用量与执行详情/);
    assert.match(html, /总输入 Token<\/small><strong>未知<\/strong>/);
    assert.ok(html.indexOf('审查单元进展') < html.indexOf('总输入 Token'));
    const loading = await render({ loaded: false, findingsLoaded: false });
    assert.match(loading, /正在读取审查单元/);
    assert.match(loading, /正在读取核验状态/);
    assert.doesNotMatch(loading, /已完成 <strong>0|暂未确认问题/);
    const unlabeled = run('completed');
    unlabeled.plan.units = [{ id: 'u1', label: null, paths: ['auth.py'] }];
    const single = await render({ task: unlabeled, units: [work('z', 'u1', 'review', 'completed')] });
    assert.equal((single.match(/auth\.py/g) || []).length, 1);
  });
});

test('取消、部分完成和连接中断分别表达；终态用时固定', async () => {
  await withServer(async server => {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
    const html = await renderToString(createSSRApp(Page, {
      task: run('cancelled'), units: [work('w', 'u1', 'review', 'interrupted')], report: null,
      syncedAt: Date.now() - 10000, syncError: '同步失败',
    }));
    assert.match(html, /审查已停止/);
    assert.match(html, /总用时 3 分 0 秒/);
    assert.match(html, /未完成审查单元/);
    assert.match(html, /相关代码不能视为已通过审查/);
    assert.match(html, /重试读取进度/);
    assert.match(html, /查看审查结果/);
    assert.doesNotMatch(html, /暂未确认问题|审查通过|正在分析|前同步/);
  });
});

test('提交页区分提交中、明确拒绝与结果未知，不伪造已创建任务', async () => {
  await withServer(async server => {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/ReviewSubmissionPage.vue');
    const creation = { key: 'same-request', input: { pr_url: 'https://github.com/o/r/pull/1' }, startedAt: new Date().toISOString() };
    const render = props => renderToString(createSSRApp(Page, { creation, submitting: false, rejected: false, error: '', ...props }));
    const pending = await render({ submitting: true });
    assert.match(pending, /正在创建审查/);
    assert.match(pending, /尚未取得任务 ID/);
    assert.doesNotMatch(pending, /任务已创建|返回修改/);
    const unknown = await render({});
    assert.match(unknown, /尚未确认创建结果/);
    assert.match(unknown, /继续确认这次请求/);
    assert.doesNotMatch(unknown, /返回修改/);
    const rejected = await render({ rejected: true });
    assert.match(rejected, /服务端未接受这次请求/);
    assert.match(rejected, /返回修改/);
  });
});

test('待确认请求可恢复；网络重试沿用同一 key 和请求体，422 不重试', async () => {
  await withServer(async server => {
    const { createReview, ApiError } = await server.ssrLoadModule('/src/api/client.ts');
    const { readPendingCreation, savePendingCreation, saveAcceptedReview, creationWasRejected } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const storage = new Map();
    const previousStorage = globalThis.localStorage, previousFetch = globalThis.fetch;
    globalThis.localStorage = {
      setItem: (key, value) => storage.set(key, value),
      getItem: key => storage.get(key) ?? null,
      removeItem: key => storage.delete(key),
    };
    try {
      const pending = {
        key: 'stable-key', startedAt: new Date().toISOString(),
        input: { pr_url: 'https://github.com/o/r/pull/1', depth: 'standard', paths: null, focus: ['correctness'], limits: {} },
      };
      savePendingCreation(pending);
      assert.deepEqual(readPendingCreation(), pending);
      const calls = [];
      globalThis.fetch = async (_path, init) => {
        calls.push(init);
        if (calls.length === 1) throw new TypeError('network interrupted');
        return Response.json({ id: 'created' });
      };
      await createReview(pending.input, undefined, readPendingCreation().key);
      assert.equal(calls.length, 2);
      assert.equal(calls[0].headers['Idempotency-Key'], 'stable-key');
      assert.equal(calls[1].headers['Idempotency-Key'], 'stable-key');
      assert.equal(calls[0].body, calls[1].body);
      let rejectedCalls = 0;
      globalThis.fetch = async () => { rejectedCalls++; return Response.json({ detail: 'invalid request' }, { status: 422 }); };
      await assert.rejects(createReview(pending.input, undefined, pending.key), error => creationWasRejected(error));
      assert.equal(rejectedCalls, 1);
      assert.equal(creationWasRejected(new ApiError('busy', 503)), false);
      assert.equal(creationWasRejected(new TypeError('network')), false);
      const setItem = globalThis.localStorage.setItem;
      globalThis.localStorage.setItem = () => { throw new Error('storage full'); };
      saveAcceptedReview('created');
      assert.deepEqual(readPendingCreation(), pending);
      assert.equal(savePendingCreation(pending), false);
      globalThis.localStorage.setItem = setItem;
      saveAcceptedReview('created');
      assert.equal(storage.get('rg-review-task'), 'created');
      assert.equal(readPendingCreation(), null);
      savePendingCreation(null);
      assert.equal(readPendingCreation(), null);
      storage.set('rg-review-creation', '{"key":"broken"}');
      assert.equal(readPendingCreation(), null);
    } finally {
      globalThis.fetch = previousFetch;
      if (previousStorage === undefined) delete globalThis.localStorage;
      else globalThis.localStorage = previousStorage;
    }
  });
});

test('长时间准备继续等待，主动中断会释放请求且不自动重复 POST', async context => {
  await withServer(async server => {
    const { createReview } = await server.ssrLoadModule('/src/api/client.ts');
    const { creationWasRejected } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const previousFetch = globalThis.fetch;
    const input = { pr_url: 'https://github.com/o/r/pull/1', depth: 'standard', paths: null, focus: ['correctness'], limits: {} };
    let calls = 0;
    let accept;
    globalThis.fetch = (_url, { signal }) => {
      calls++;
      return new Promise((resolve, reject) => {
        accept = resolve;
        const fail = () => reject(new DOMException('Aborted', 'AbortError'));
        if (signal.aborted) fail();
        else signal.addEventListener('abort', fail, { once: true });
      });
    };
    try {
      context.mock.timers.enable({ apis: ['setTimeout'] });
      let settled = false;
      const waiting = createReview(input, undefined, 'slow-key');
      void waiting.then(() => { settled = true; });
      context.mock.timers.tick(193000);
      await Promise.resolve();
      assert.equal(settled, false);
      assert.equal(calls, 1);
      accept(Response.json({ id: 'created-after-preparation' }));
      assert.equal((await waiting).id, 'created-after-preparation');
      context.mock.timers.reset();
      const controller = new AbortController();
      const interrupted = createReview(input, controller.signal, 'abort-key');
      controller.abort();
      await assert.rejects(interrupted, error => error.name === 'AbortError' && !creationWasRejected(error));
      assert.equal(calls, 2);
    } finally {
      context.mock.timers.reset();
      globalThis.fetch = previousFetch;
    }
  });
});

test('源码准备错误展示安全消息并遵守服务端的重试标记', async () => {
  await withServer(async server => {
    const { createReview, ApiError } = await server.ssrLoadModule('/src/api/client.ts');
    const { creationWasRejected } = await server.ssrLoadModule('/src/utils/reviewCreation.ts');
    const previousFetch = globalThis.fetch;
    const input = { pr_url: 'https://github.com/o/r/pull/1', depth: 'standard', paths: null, focus: ['correctness'], limits: {} };
    let calls = 0;
    try {
      globalThis.fetch = async () => {
        calls++;
        return Response.json({ detail: { code: 'git_snapshot_unavailable',
          message: '无法获取本次 PR 的固定提交', retryable: false } }, { status: 502 });
      };
      await assert.rejects(createReview(input, undefined, 'missing-sha'), error =>
        error instanceof ApiError && error.message === '无法获取本次 PR 的固定提交'
          && error.retryable === false && creationWasRejected(error));
      assert.equal(calls, 1);
      calls = 0;
      globalThis.fetch = async () => {
        calls++;
        return Response.json({ detail: { code: 'git_connection_failed',
          message: '连接源码仓库失败', retryable: true } }, { status: 503 });
      };
      await assert.rejects(createReview(input, undefined, 'connection'), error =>
        error instanceof ApiError && error.retryable === true && !creationWasRejected(error));
      assert.equal(calls, 2);
    } finally { globalThis.fetch = previousFetch; }
  });
});
