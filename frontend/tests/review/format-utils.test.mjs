import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

test('格式化审查耗时与文件容量', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { formatDuration } = await server.ssrLoadModule('/src/utils/formatDuration.ts');
    const { formatBytes } = await server.ssrLoadModule('/src/utils/formatBytes.ts');
    assert.equal(formatDuration(45), '45s');
    assert.equal(formatDuration(90), '1m');
    assert.equal(formatDuration(3720), '1h 2m');
    assert.equal(formatBytes(512), '512 B');
    assert.equal(formatBytes(2048), '2.0 KB');
    assert.equal(formatBytes(3 * 1024 * 1024), '3.0 MB');
  } finally {
    await server.close();
  }
});
