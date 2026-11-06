import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'vite';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

test('侧栏随主题切换新版品牌图标，资源与提供的原稿一致', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, optimizeDeps: { noDiscovery: true, entries: [], include: [] }, appType: 'custom' });
  let theme, previousTheme;
  try {
    const { default: Sidebar } = await server.ssrLoadModule('/src/components/layout/AppSidebar.vue');
    const { usePreferences } = await server.ssrLoadModule('/src/composables/usePreferences.ts');
    theme = usePreferences().theme;
    previousTheme = theme.value;
    for (const [mode, asset, original] of [
      ['light', 'rivu-symbol-color.svg', 'rivu-symbol.svg'],
      ['dark', 'rivu-symbol-white.svg', 'rivu-symbol-white.svg'],
    ]) {
      theme.value = mode;
      const html = await renderToString(createSSRApp(Sidebar, {
        modelValue: 'dashboard', section: 'overview', hasReview: false,
        taskActive: false, mobileOpen: false, busy: false,
      }));
      const { default: expectedSource } = await server.ssrLoadModule(`/src/assets/${asset}`);
      const actualSource = /<img src="([^"]+)"/.exec(html)?.[1].replaceAll('&#39;', "'");
      assert.equal(actualSource, expectedSource);
      assert.match(html, /<strong>Rivu<\/strong>/);
      assert.equal((await readFile(new URL(`../../src/assets/${asset}`, import.meta.url), 'utf8')).trimEnd(),
        (await readFile(new URL(`../../../pic/logo/${original}`, import.meta.url), 'utf8')).trimEnd());
    }
  } finally {
    if (theme) theme.value = previousTheme;
    await server.close();
  }
});

test('网页与安装图标使用新版素材，并按实际尺寸声明', async () => {
  const html = await readFile(new URL('../../index.html', import.meta.url), 'utf8');
  const favicon = await readFile(new URL('../../public/favicon.svg', import.meta.url), 'utf8');
  assert.equal(favicon.trimEnd(), (await readFile(new URL('../../../pic/logo/rivu-app-icon.svg', import.meta.url), 'utf8')).trimEnd());
  for (const asset of ['rivu-favicon-64.png', 'rivu-app-icon-1024.png']) {
    const image = await readFile(new URL(`../../public/${asset}`, import.meta.url));
    assert.deepEqual(image, await readFile(new URL(`../../../pic/logo/${asset}`, import.meta.url)));
    const size = asset.includes('1024') ? 1024 : 64;
    assert.equal(image.readUInt32BE(16), size);
    assert.equal(image.readUInt32BE(20), size);
    assert.ok(html.includes(`href="/${asset}"`));
    assert.ok(html.includes(`sizes="${size}x${size}"`));
  }
  assert.match(html, /href="\/favicon\.svg\?v=2"/);
  assert.doesNotMatch(html, /favicon\.ico|\/apple-touch-icon\.png/);
  const manifest = JSON.parse(await readFile(new URL('../../public/site.webmanifest', import.meta.url), 'utf8'));
  for (const icon of manifest.icons) {
    assert.equal(icon.purpose, 'any');
    await readFile(new URL(`../../public${icon.src.split('?')[0]}`, import.meta.url));
  }
  assert.equal(manifest.icons.find(icon => icon.type === 'image/png').sizes, '1024x1024');
});
