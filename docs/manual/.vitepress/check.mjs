import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { digest, files, inputs, output } from './inputs.mjs';

export async function check(directory = output) {
  const manifest = JSON.parse(await readFile(path.join(directory, 'build.json'), 'utf8'));
  assert.deepEqual(manifest.inputs, await inputs(), 'Manual sources changed. Run npm --prefix docs/manual run build.');
  const inventory = (await files(directory)).filter(file => path.basename(file) !== 'build.json');
  assert.deepEqual(inventory.map(file => path.relative(directory, file).split(path.sep).join('/')).sort(), Object.keys(manifest.files).sort(), 'Missing or leftover manual output');
  for (const file of inventory) {
    const name = path.relative(directory, file).split(path.sep).join('/');
    assert.equal(digest(await readFile(file)), manifest.files[name], 'Changed generated manual asset: ' + name);
    if (!name.endsWith('.html')) continue;
    const html = await readFile(file, 'utf8');
    const current = new URL(manifest.base + name, 'https://manual.invalid');
    for (const match of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
      const raw = match[1].replaceAll('&amp;', '&');
      const url = new URL(raw, current);
      if (url.origin !== current.origin || url.pathname === '/') continue;
      assert.ok(url.pathname.startsWith(manifest.base), 'Unexpected local manual URL: ' + raw);
      let local = decodeURIComponent(url.pathname.slice(manifest.base.length));
      if (!local || local.endsWith('/')) local += 'index.html';
      const target = path.resolve(directory, local);
      assert.ok(target.startsWith(path.resolve(directory) + path.sep), 'Manual path escaped output');
      assert.ok((await stat(target)).isFile(), 'Missing manual link: ' + local);
      if (url.hash && local.endsWith('.html')) {
        const targetHtml = await readFile(target, 'utf8');
        const anchor = decodeURIComponent(url.hash.slice(1));
        assert.ok(targetHtml.includes('id="' + anchor + '"'), `Missing manual section: ${local}#${anchor}`);
      }
    }
  }
  console.log(`Manual verified: ${manifest.version}, ${inventory.length} files, three languages, local links and assets.`);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await check();
