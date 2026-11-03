import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const sha = char => char.repeat(40);
const task = status => ({ id: 'review', status, phase: status, pr_url: 'https://github.com/o/r/pull/1', pr: { owner: 'o', repo: 'r', number: 1, title: '<script>alert(1)</script>', body: null, html_url: 'https://github.com/o/r/pull/1' }, snapshot: { review_id: 'review', repository: 'o/r', repository_id: 1, pr_number: 1, declared_base_sha: sha('a'), base_sha: sha('b'), head_sha: sha('c') }, configuration: { provider: 'fixture', endpoint: '', model: 'fixture', options: { depth: 'standard', paths: null, focus: ['correctness'], include_pr_discussions: false, limits: {} }, prompt_version: 'review-vnext-1' }, changed_files: [], plan: { version: 'hunk-plan-v1', units: [], excluded: {}, incremental_from: null, history: [] }, report: null, review_cutoff: '', hard_deadline: '', created_at: '', updated_at: '', started_at: null, completed_at: null, error_code: status === 'failed' ? 'TimeoutError' : null });

test('结构化缺口与历史未决项保持未完成，安全展示具体阻塞原因', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
    const { progressSummary } = await server.ssrLoadModule('/src/utils/reviewProgress.ts');
    const run = task('partial');
    run.plan.units = [{ id: 'unit', paths: ['a.py'] }];
    const unit = { id: 'work', kind: 'review', unit_id: 'unit', status: 'completed',
      attempt: 1, material_delivery: 'complete', tool_calls: 0, error_code: null,
      missing_material_reasons: {}, result: null };
    const gap = {
      required_check: '确认输入契约', observation: '源码使用输入作为除数',
      missing_premise: '<script>未知的输入约束</script>', why_required: '合法输入决定失败是否可达',
      source_refs: ['S1'],
    };
    for (const result of [{ unresolved: gap }, { unresolved_reason: '历史未决项仍需检查' }]) {
      unit.result = result;
      assert.equal(progressSummary(run, [unit], []).completed, 0);
      assert.equal(progressSummary(run, [unit], []).unavailable, 1);
      const html = await renderToString(createSSRApp(Component, { task: run, units: [unit], report: null }));
      assert.match(html, /存在未完成检查/);
      assert.doesNotMatch(html, /<script>/);
      if (result.unresolved) {
        assert.match(html, /必要检查：确认输入契约/);
        assert.match(html, /缺失事实：&lt;script&gt;未知的输入约束&lt;\/script&gt;/);
        assert.match(html, /阻塞原因：合法输入决定失败是否可达/);
      } else {
        assert.match(html, /历史未决项仍需检查/);
      }
    }
    unit.result = { unresolved: null };
    assert.equal(progressSummary(run, [unit], []).completed, 1);
    assert.equal(progressSummary(run, [unit], []).unavailable, 0);
  } finally { await server.close(); }
});

test('预算耗尽说明具体限制，避免显示为未知执行异常', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { executionErrorLabel } = await server.ssrLoadModule('/src/utils/nativeReview.ts');
    assert.equal(executionErrorLabel('agent_model_limit'), '达到单个 Agent 的模型调用上限，调查尚未完成');
    assert.equal(executionErrorLabel('agent_structured_output_missing'), '模型未返回有效的结构化结果，任务尚未完成');
    assert.equal(executionErrorLabel('agent_execution_failed'), 'Agent 执行异常，未取得有效结果');
    for (const code of ['model_call_limit', 'input_token_limit', 'output_token_limit',
      'context_capacity', 'tool_call_limit', 'agent_tool_loop', 'review_cutoff', 'hard_deadline']) {
      assert.doesNotMatch(executionErrorLabel(code), /执行异常/);
    }
  } finally { await server.close(); }
});

test('活动页区分冻结语义分组并安全显示 Unit 标签', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
    const run = task('running');
    run.plan.grouping_status = 'model';
    run.plan.units = [{ id: 'unit', label: '<script>Feature</script>', paths: ['auth/service.py'] }];
    const html = await renderToString(createSSRApp(Component, {
      task: run, report: null, units: [{ id: 'work', kind: 'review', unit_id: 'unit',
        status: 'interrupted', attempt: 1, material_delivery: 'complete', tool_calls: 4,
        error_code: 'agent_tool_loop', missing_material_reasons: {} }],
    }));
    assert.match(html, /语义分组已冻结/);
    assert.match(html, /auth\/service.py/);
    assert.match(html, /&lt;script&gt;Feature&lt;\/script&gt;/);
    assert.match(html, /持续重复相同工具调用，未完成结构化收尾/);
    assert.doesNotMatch(html, /<script>/);
  } finally { await server.close(); }
});

test('活动页显示输入缓存细分，缺失明细保持未知', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewActivityPage.vue');
    const { observedUsage } = await server.ssrLoadModule('/src/utils/nativeReview.ts');
    const report = { usage: {
      model_calls: 10, input_tokens: 10000, cached_input_tokens: 8000,
      uncached_input_tokens: 2000, output_tokens: 500, cache_hit_rate: 0.8,
      cache_unknown_calls: 0,
    }, reasons: [] };
    const html = await renderToString(createSSRApp(Component, {
      task: task('completed'), report, units: [],
    }));
    assert.match(html, /总输入 Token/);
    assert.match(html, /10,000/);
    assert.match(html, /8,000/);
    assert.match(html, /2,000/);
    assert.match(html, /输出 Token/);
    assert.match(html, /命中率 80.0%/);
    assert.equal(observedUsage(report).tokens, 10500);
    report.usage.cached_input_tokens = null;
    report.usage.uncached_input_tokens = null;
    report.usage.cache_hit_rate = null;
    report.usage.cache_unknown_calls = 1;
    const unknown = await renderToString(createSSRApp(Component, {
      task: task('completed'), report, units: [],
    }));
    assert.match(unknown, /命中率未知/);
    assert.match(unknown, /缺少供应商缓存明细/);
    assert.doesNotMatch(unknown, /命中率 0.0%/);
  } finally { await server.close(); }
});

test('新运行投影保留终态、快照和安全链接语义', async () => { const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' }); try { const { terminal, phaseLabel, reviewReasonLabel, safeGitHubLink, isFileContextFor } = await server.ssrLoadModule('/src/utils/nativeReview.ts'); assert.equal(terminal('partial'), true); assert.equal(terminal('running'), false); assert.equal(phaseLabel('finished'), '审查结束'); assert.equal(reviewReasonLabel('unverified_historical_candidates'), '仍有增量复核项未完成。'); assert.equal(safeGitHubLink('javascript:alert(1)'), undefined); assert.equal(safeGitHubLink('https://github.com/o/r/pull/1'), 'https://github.com/o/r/pull/1'); assert.equal(isFileContextFor({ file_id: 0, file_path: 'a.py', snapshot: task('completed').snapshot, base_lines: [], head_lines: [] }, 0, 'a.py', task('completed').snapshot), true); } finally { await server.close(); } });
test('五切片工作台展示运行中和失败状态，不渲染不可信 HTML', async () => { const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' }); try { const { default: Component } = await server.ssrLoadModule('/src/components/review/ReviewWorkspace.vue'); for (const status of ['running', 'failed', 'cancelled']) { const html = await renderToString(createSSRApp(Component, { task: task(status) })); assert.match(html, /概览|变更|问题|活动|发布/); assert.match(html, /未执行目标代码/); assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>alert/); if (status === 'running') assert.match(html, /停止审查/); } } finally { await server.close(); } });

test('总览经 JSON 读取后保留自然段和行内代码，只把其他标记作为文本', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewOverviewPage.vue');
    const report = JSON.parse(JSON.stringify({
      version: 'd'.repeat(64), status: 'completed', findings: [], candidates: [], units: [],
      excluded: {}, reasons: [], usage: {},
      overview: {
        status: 'ready', input_hash: 'e'.repeat(64), error_code: null, sources: [],
        omitted: { changes: 1, sources: 2, findings: 0, decision_boundaries: 0 },
        omitted_materials: [{ kind: 'change', unit_id: 'unit', member_id: 'hunk',
          path: '<script>unsafe.py</script>', side: 'head', start_line: 437, end_line: 459,
          reason: 'material_capacity' }],
        output: { finding_order: [], change_overview: [
          { text: '调整 `weighted_posterior` 的区间边界。\r\n\r\n不收窄原区间。', source_refs: ['S1'] },
          { text: '补充边界参数化测试和中间值对照断言。', source_refs: ['S2'] },
          { text: '<img src=x onerror=alert(1)> [链接](javascript:alert(1)) `a<b` *强调*', source_refs: ['S1'] },
        ] },
      },
    }));
    const html = await renderToString(createSSRApp(Component, {
      task: task('completed'), report, findings: [], loading: false,
    }));
    assert.match(html, /<code>weighted_posterior<\/code>/);
    assert.match(html, /区间边界。\r\n\r\n不收窄原区间。/);
    assert.match(html, /<p class="rg-preserve-lines">.*补充边界参数化测试和中间值对照断言。/);
    assert.match(html, /&lt;img/);
    assert.match(html, /<code>a&lt;b<\/code>/);
    assert.match(html, /\*强调\*/);
    assert.doesNotMatch(html, /<img|<script>|<a[^>]+javascript:|<em>/);
    assert.match(html, /查看总览省略范围/);
    assert.match(html, /HEAD 437–459/);
    assert.match(html, /材料容量不足/);
    assert.match(html, /&lt;script&gt;unsafe.py&lt;\/script&gt;/);
  } finally { await server.close(); }
});

test('主动跳过总览有独立说明且不显示失败重试', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewOverviewPage.vue');
    const report = {
      candidates: [], excluded: {}, findings: [], reasons: [], units: [], usage: {},
      overview: { status: 'skipped_disabled', output: null, omitted: {}, omitted_materials: [] },
    };
    const html = await renderToString(createSSRApp(Component, {
      task: task('completed'), report, findings: [], loading: false,
    }));
    assert.match(html, /本次审查已设置跳过总览/);
    assert.doesNotMatch(html, /使用剩余额度重试总览|变更总览未能生成/);
  } finally { await server.close(); }
});

test('增量概览说明范围复用但不展示历史意见', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewOverviewPage.vue');
    const incremental = task('completed');
    incremental.plan.incremental_from = 'previous-review';
    incremental.plan.history = [{ claim: 'OLD CLAIM', candidate_id: 'old' }];
    const report = {
      candidates: [], excluded: {}, findings: [], reasons: ['unverified_historical_candidates'],
      units: [], usage: {}, overview: { status: 'pending', omitted: {}, omitted_materials: [] },
    };
    const html = await renderToString(createSSRApp(Component, {
      task: incremental, report, findings: [], loading: false,
    }));
    assert.match(html, /审查单元与复用/);
    assert.match(html, /复用范围内的候选仍须本轮独立核验/);
    assert.match(html, /仍有增量复核项未完成/);
    assert.doesNotMatch(html, /历史意见|OLD CLAIM|unverified_historical_candidates/);
  } finally {
    await server.close();
  }
});

test('行内代码组件不把围栏、跨行反引号、HTML 和链接当作可执行内容', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/common/InlineCodeText.vue');
    for (const text of ['```shell\nalert(1)\n```', '`跨\n行`', '``双反引号``']) {
      const html = await renderToString(createSSRApp(Component, { text }));
      assert.doesNotMatch(html, /<code>|<pre>/);
    }
    const html = await renderToString(createSSRApp(Component, {
      text: '`<script>alert(1)</script>` [外链](javascript:alert(1))',
    }));
    assert.match(html, /<code>&lt;script&gt;alert\(1\)&lt;\/script&gt;<\/code>/);
    assert.doesNotMatch(html, /<script>|<a /);
  } finally { await server.close(); }
});

test('疑似问题区分引用校验失败和业务前提不足，安全展示核实问题', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Component } = await server.ssrLoadModule('/src/components/pages/ReviewFindingsPage.vue');
    const candidate = {
      id: 'invalid', proposal: { claim: '字典被原地修改', location: { path: 'stats.py', line: 27, side: 'head' } },
      sources: [{}], verification_sources: [], admission_reason: 'invalid_source_refs',
      verification: { verdict: 'insufficient', reason: '引用混入 R1', question: null },
    };
    const question = {
      ...candidate, id: 'question', admission_reason: null, verification_sources: [{}],
      verification: { verdict: 'insufficient', reason: '输入约定未确定', question: {
        observation: '源码含有 <script>untrusted</script>',
        missing_premise: '空列表是否是有效输入',
        question: '请确认是否允许 files=[]',
      } },
    };
    const html = await renderToString(createSSRApp(Component, {
      task: task('completed'), findings: [], candidates: [candidate, question],
    }));
    assert.match(html, /复核结果校验失败/);
    assert.match(html, /invalid_source_refs/);
    assert.match(html, /这不表示源码一定缺失/);
    assert.match(html, /缺失前提：空列表是否是有效输入/);
    assert.match(html, /请确认：请确认是否允许 files=\[\]/);
    assert.match(html, /&lt;script&gt;untrusted&lt;\/script&gt;/);
    assert.doesNotMatch(html, /<script>|低风险/);
  } finally { await server.close(); }
});
