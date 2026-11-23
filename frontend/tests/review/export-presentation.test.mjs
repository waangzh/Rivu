import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

test('导出格式标签与文件名解析', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { exportFormatLabel, exportFilenameFromDisposition } = await server.ssrLoadModule('/src/utils/exportPresentation.ts');
    assert.equal(exportFormatLabel('csv'), 'CSV 表格');
    assert.equal(exportFormatLabel('unknown'), 'unknown');
    assert.equal(exportFilenameFromDisposition('attachment; filename="rivu.md"'), 'rivu.md');
    assert.equal(exportFilenameFromDisposition(null), null);
  } finally {
    await server.close();
  }
});
