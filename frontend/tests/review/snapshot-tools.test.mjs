import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

test('快照检索结果解析与展示', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [], include: [] },
    appType: 'custom',
  });
  try {
    const { parseSnapshotHits, snapshotHitLabel } = await server.ssrLoadModule('/src/utils/snapshotTools.ts');
    const hits = parseSnapshotHits({ hits: [
      { path: 'app/a.py', line: 3, text: 'needle' },
      { path: 'app/b.py', line: 9, text: 'other' },
    ] });
    assert.equal(hits.length, 2);
    assert.equal(snapshotHitLabel(hits[0]), 'app/a.py:3 needle');
    assert.deepEqual(parseSnapshotHits(null), []);
  } finally {
    await server.close();
  }
});
