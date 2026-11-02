import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const task = {
  id: 'report', status: 'completed', phase: 'report',
  snapshot: { head_sha: 'a'.repeat(40), base_sha: 'b'.repeat(40), declared_base_sha: 'c'.repeat(40) },
};

test('发布页正文去掉重复的 Rivu 审查评论标题，保留其后内容', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: Page } = await server.ssrLoadModule('/src/components/pages/ReviewReportPage.vue');
    const report = '# Rivu 审查评论\n\n### 审查结论\n\n审查完成。';
    const html = await renderToString(createSSRApp(Page, { task, plan: null, report, loading: false }));
    assert.doesNotMatch(html, /Rivu 审查评论/);
    assert.match(html, /审查结论/);
    assert.match(html, /审查完成/);
  } finally {
    await server.close();
  }
});
