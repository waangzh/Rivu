import test from 'node:test';
import assert from 'node:assert/strict';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { createServer } from 'vite';

test('报告摘要展示发现数量与审查耗时', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { default: ReportSummaryBar } = await server.ssrLoadModule('/src/components/report/ReportSummaryBar.vue');
    const html = await renderToString(createSSRApp(ReportSummaryBar, {
      summary: {
        report: null,
        markdown: '',
        findings_count: 3,
        duration_seconds: 42,
        generated_at: null,
      },
    }));
    assert.match(html, /3 条已确认发现/);
    assert.match(html, /42 秒/);
  } finally {
    await server.close();
  }
});
