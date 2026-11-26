import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { build } from 'vite';
import { createRenderer, h, nextTick, shallowRef } from 'vue';

// Use the production client templates with a Vue host that records elements and events.
const entries = {
  App: 'App.vue',
  Page: 'components/pages/ReviewReportPage.vue',
  Workspace: 'components/review/ReviewWorkspace.vue',
  Panel: 'components/review/publication/GitHubPublicationPanel.vue',
};
const bundle = await build({
  configFile: false,
  logLevel: 'silent',
  plugins: [vue({ template: { compilerOptions: { hoistStatic: false } } }), {
    name: 'interaction-entry',
    enforce: 'pre',
    resolveId: id => id.endsWith('/interaction-entry') || id === 'interaction-entry' ? '\0interaction-entry' : null,
    load: id => id === '\0interaction-entry' ? Object.entries(entries).map(([name, path]) =>
      `export { default as ${name} } from ${JSON.stringify(fileURLToPath(new URL('../../src/' + path, import.meta.url)))};`).join('\n') : null,
  }],
  build: {
    write: false,
    lib: { entry: 'interaction-entry', name: 'InteractionTest', formats: ['es'] },
    rollupOptions: { external: ['vue'], output: { paths: { vue: import.meta.resolve('vue') } } },
  },
});
const code = bundle[0].output.find(output => output.type === 'chunk').code;
const directory = await mkdtemp(join(tmpdir(), 'rivu-interactions-'));
const { App, Page, Workspace, Panel } = await (async () => {
  try {
    const filename = join(directory, 'components.mjs');
    await writeFile(filename, code);
    return await import(pathToFileURL(filename));
  } finally {
    await rm(directory, { recursive: true });
  }
})();

function node(tag, text = '') {
  return { tag, text, children: [], props: {}, listeners: {},
    getRootNode() { return null; },
    addEventListener(name, callback) { this.listeners[name] = callback; } };
}
function remove(item) {
  if (!item.parent) return;
  item.parent.children.splice(item.parent.children.indexOf(item), 1);
  item.parent = null;
}
const renderer = createRenderer({
  createElement: tag => tag === 'dialog' ? { ...node(tag), close() {} } : node(tag),
  createText: text => node('#text', text),
  createComment: () => node('#comment'),
  setText: (item, text) => { item.text = text; },
  setElementText: (item, text) => { item.children = []; item.text = text; },
  parentNode: item => item.parent,
  nextSibling: item => item.parent?.children[item.parent.children.indexOf(item) + 1],
  patchProp: (item, key, _before, value) => { item.props[key] = value; },
  remove,
  insert(item, parent, anchor) {
    remove(item);
    parent.children.splice(anchor ? parent.children.indexOf(anchor) : parent.children.length, 0, item);
    item.parent = parent;
  },
});
const all = (item, predicate) => [...(predicate(item) ? [item] : []), ...item.children.flatMap(child => all(child, predicate))];
const text = item => (item.props.innerHTML ?? item.text) + item.children.map(text).join('');
const settle = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick(); };
function mount(t, view, initial = {}) {
  const root = node('root'), props = shallowRef(initial);
  const app = renderer.createApp({ render: () => h(view, props.value) });
  app.config.errorHandler = error => { throw new Error(error.message); };
  app.mount(root);
  t.after(() => app.unmount());
  const button = label => all(root, item => item.tag === 'button' && text(item).trim() === label)[0];
  return {
    text: () => text(root), button,
    click: label => click(button(label)),
    rows: () => all(root, item => item.props.class === 'gp-record__row'),
    set: async value => { props.value = { ...props.value, ...value }; await settle(); },
    enter(value) {
      const input = all(root, item => item.tag === 'input' && item.props.type === 'password')[0];
      input.value = value;
      input.listeners.input({ target: input });
    },
  };
}
function click(button) {
  assert.ok(button, 'button exists');
  assert.ok(!button.props.disabled, 'button is enabled');
  button.props.onClick({ target: button, currentTarget: button });
}
function mockGlobal(t, name, value) {
  const prior = Object.getOwnPropertyDescriptor(globalThis, name);
  Object.defineProperty(globalThis, name, { configurable: true, value });
  t.after(() => prior ? Object.defineProperty(globalThis, name, prior) : delete globalThis[name]);
}
function deferredFetch(t, other = () => undefined) {
  const requests = [];
  t.mock.method(globalThis, 'fetch', (url, init) => other(url, init) ?? new Promise(resolve => {
    requests.push({ url, init, reply: (data, status) => resolve(Response.json(data, { status })) });
  }));
  return requests;
}
const head = 'a'.repeat(40);
const task = (id, status = 'completed', version = 'v1') => ({
  id, status, updated_at: 'same-time', phase: 'report',
  report: version ? { version } : null, plan: { units: [] }, changed_files: [], configuration: { model: 'fixture' },
  snapshot: { head_sha: head, base_sha: head, declared_base_sha: head, repository: 'demo/repo' },
});
const plan = (id, version = 'v1') => ({
  review_id: id, report_version: version, snapshot: { head_sha: head },
  digest: { execution_status: 'completed' }, inline_drafts: [], summary_findings: [],
  review_comment_markdown: `评论 ${id} ${version}`,
});
const findings = version => ({ report_version: version, findings: [], candidates: [], finding_groups: [] });
const progress = () => Response.json({ units: [], usage: {} });

test('实际复制按钮检查 Head 与正文，切换任务清除迟到响应', async t => {
  const writes = [];
  mockGlobal(t, 'navigator', { clipboard: { writeText: async text => writes.push(text) } });
  const requests = deferredFetch(t, url => url.endsWith('/publication-capability')
    ? Promise.resolve(Response.json({ enabled: false })) : undefined);
  const page = mount(t, Page, { task: task('first'), plan: plan('first'), report: '导出报告', loading: false });
  function respond(status, preview = '评论 first v1') {
    const { url, init, reply } = requests.at(-1);
    assert.match(url, /review-comment-check$/);
    assert.deepEqual(JSON.parse(init.body), { detail: 'compact', exclude_resolved: false });
    reply({
      review_id: url.split('/').at(-2), reviewed_head_sha: head, latest_head_sha: status === 'current' ? head : null,
      status, detail: 'compact', preview_markdown: preview,
      review_comment_markdown: status === 'current' ? preview : preview + ' · 快照说明',
      included_finding_keys: [], omitted_finding_keys: [], retained_blocking_keys: [],
    });
  }
  const confirm = '确认复制带快照说明的评论';
  assert.match(page.text(), /评论 first v1/);
  page.click('复制评论'); assert.equal(writes.length, 0);
  respond('current'); await settle();
  assert.deepEqual(writes, ['评论 first v1']);
  page.click('已复制'); respond('stale'); await settle();
  assert.equal(writes.length, 1);
  page.click('取消复制'); await settle();
  assert.equal(page.button(confirm), undefined);
  page.click('复制评论'); respond('unknown'); await settle();
  assert.equal(writes.length, 1);
  page.click(confirm); await settle();
  assert.equal(writes.at(-1), '评论 first v1 · 快照说明');
  page.click('已复制'); respond('current', '变更后的正文'); await settle();
  assert.equal(writes.length, 2);
  assert.match(page.text(), /变更后的正文/);
  page.click('确认复制更新后的评论'); respond('stale', '变更后的正文'); await settle();
  assert.equal(writes.length, 2);
  page.click(confirm); await settle();
  assert.equal(writes.at(-1), '变更后的正文 · 快照说明');

  page.click('已复制');
  const old = requests.at(-1);
  await page.set({ task: task('second'), plan: plan('second') });
  assert.equal(old.init.signal.aborted, true);
  respond('current', '变更后的正文'); await settle();
  assert.equal(writes.length, 3);
  assert.doesNotMatch(page.text(), /变更后的正文/);
  await page.set({ task: { ...task('local'), source: { kind: 'local_git' } }, plan: plan('local') });
  page.click('复制评论'); await settle();
  assert.equal(writes.at(-1), '评论 local v1');
  assert.equal(requests.length, 6);
});

test('工作台只显示同版本结果，独立重试计划，活跃审查继续刷新', async t => {
  const calls = [];
  let version = 'v1', failPlan = true, delay = false, release;
  t.mock.method(globalThis, 'fetch', async (url, { signal } = {}) => {
    if (url.endsWith('/publication-capability')) return Response.json({ enabled: false });
    const [, , , id, endpoint] = url.split('/');
    calls.push({ endpoint, signal });
    if (endpoint === 'automatic-publication') return Response.json(null);
    if (endpoint === 'progress') return progress();
    if (endpoint === 'findings') return delay && id === 'first'
      ? new Promise(resolve => { release = resolve; }) : Response.json(findings(version));
    if (endpoint === 'report') return Response.json({ report: { version }, markdown: `报告 ${id} ${version}`, report_version: version });
    return failPlan ? Response.json({}, { status: 503 }) : Response.json(plan(id, version));
  });
  const first = task('first');
  const page = mount(t, Workspace, { task: first, section: 'publish' });
  const count = endpoint => calls.filter(call => call.endpoint === endpoint).length;
  await settle();
  assert.match(page.text(), /报告 first v1/);
  failPlan = false;
  page.click('重试读取发布计划'); await settle();
  assert.equal(page.button('复制评论').props.disabled, false);
  await page.set({ task: { ...first } });
  assert.deepEqual(['findings', 'report', 'publication-plan'].map(count), [1, 1, 2]);

  version = 'v3';
  await page.set({ task: task('first', 'completed', 'v2') });
  assert.doesNotMatch(page.text(), /报告 first v[13]/);
  assert.equal(page.button('复制评论').props.disabled, true);
  await page.set({ task: task('first', 'completed', 'v3') });
  assert.match(page.text(), /报告 first v3/);
  version = null;
  await page.set({ task: task('first', 'queued', null) });
  assert.equal(page.button('复制评论').props.disabled, true);
  const before = ['progress', 'findings'].map(count);
  await page.set({ task: task('first', 'running', null) });
  assert.deepEqual(['progress', 'findings'].map(count), before.map(value => value + 1));

  delay = true;
  await page.set({ task: task('first', 'running', null) });
  const old = calls.at(-1);
  assert.equal(old.endpoint, 'findings');
  version = 'v4';
  await page.set({ task: task('second', 'completed', 'v4') });
  assert.equal(old.signal.aborted, true);
  release(Response.json({ ...findings(null), findings: [1] })); await settle();
  assert.match(page.text(), /报告 second v4/);
  assert.doesNotMatch(page.text(), /问题 \(1\)/);
});

test('交付历史与发布轮询并行，读取失败重试，切换记录和版本清除旧请求', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  mockGlobal(t, 'Document', class {});
  mockGlobal(t, 'ShadowRoot', class {});
  const requests = deferredFetch(t);
  const page = mount(t, Panel, { reviewId: 'first', plan: plan('first'), capability: { enabled: true } });
  const attempt = (id, state) => ({ id, state, review_id: 'first', items: [], requested_at: id });
  const a = attempt('A', 'publishing'), b = attempt('B', 'failed');
  const reply = (data, status) => requests.at(-1).reply(data, status);
  page.enter('fixture'); await settle();
  page.click('刷新交付记录'); reply([a, b]); await settle();
  click(page.rows()[0]); reply(a); await settle();
  page.click('刷新交付记录');
  const history = requests.at(-1);
  t.mock.timers.tick(1500); await settle();
  assert.match(requests.at(-1).url, /\/A$/);
  assert.equal(history.init.signal.aborted, false);
  reply({}, 503); await settle();
  assert.match(page.text(), /暂时无法读取发布状态/);
  history.reply([a, b]); await settle();
  t.mock.timers.tick(3000); await settle();
  const old = requests.at(-1);
  click(page.rows()[1]);
  assert.equal(old.init.signal.aborted, true);
  reply(b); await settle();
  old.reply(attempt('A', 'succeeded')); await settle();

  const recover = page.button('重新检查并处理剩余项目');
  click(recover); click(recover);
  assert.equal(requests.filter(request => request.url.endsWith('/retry')).length, 1);
  reply(attempt('B', 'queued')); await settle();
  t.mock.timers.tick(1500); await settle();
  const obsolete = requests.at(-1);
  assert.match(obsolete.url, /\/B$/);
  await page.set({ plan: plan('first', 'v2') });
  assert.equal(obsolete.init.signal.aborted, true);
  obsolete.reply(attempt('B', 'succeeded')); await settle();
  assert.doesNotMatch(page.text(), /发布结果/);
});

test('SSE 断连降级到 REST，重连的进度事件继续触发回读', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  mockGlobal(t, 'document', { querySelector: () => null });
  mockGlobal(t, 'localStorage', { getItem: key => key === 'rg-review-task' ? 'saved' : null, setItem() {} });
  const streams = [];
  mockGlobal(t, 'EventSource', class {
    constructor() { this.listeners = {}; streams.push(this); }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    close() { this.closed = true; }
  });
  let reads = 0, offline = true;
  t.mock.method(globalThis, 'fetch', async url => {
    if (url.endsWith('/reviews/saved')) {
      reads += 1;
      if (offline) throw new Error('offline');
      return Response.json(task('saved', 'running', null));
    }
    if (url.endsWith('/progress')) return progress();
    if (url.endsWith('/findings')) return Response.json(findings(null));
    return Response.json(null);
  });
  mount(t, App); await settle();
  assert.equal(reads, 1);
  assert.equal(streams.length, 0);
  offline = false;
  t.mock.timers.tick(3000); await settle();
  assert.equal(reads, 2);
  assert.equal(streams.length, 1);
  streams[0].onerror(); await settle();
  assert.equal(reads, 3);
  assert.equal(streams[0].closed, undefined);
  t.mock.timers.tick(1500); await settle();
  assert.equal(reads, 4);
  streams[0].listeners.progress(); await settle();
  assert.equal(reads, 5);
});
