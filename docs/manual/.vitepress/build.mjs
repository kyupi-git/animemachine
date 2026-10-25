import assert from 'node:assert/strict';
import { cp, mkdir, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { build } from 'vitepress';
import { check } from './check.mjs';
import { digest, files, inputs, output, root, source } from './inputs.mjs';

const stage = path.join(root, '.local/manual-site');
const backup = path.join(root, '.local/manual-previous');
for (const directory of [stage, backup, output]) {
  assert.ok(path.resolve(directory).startsWith(path.resolve(root) + path.sep));
}
const initial = await inputs();
await rm(stage, { recursive: true, force: true });
await build(source, { outDir: stage });
await mkdir(path.join(stage, 'downloads'), { recursive: true });
await cp(path.join(root, 'deploy/compose/compose.yaml'), path.join(stage, 'downloads/compose.yaml'));
const licenses = [];
const collectLicenses = async directory => {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
    const folder = path.join(directory, entry.name);
    if (entry.name.startsWith('@')) { await collectLicenses(folder); continue; }
    const names = await readdir(folder);
    if (!names.includes('package.json')) continue;
    const pkg = JSON.parse(await readFile(path.join(folder, 'package.json'), 'utf8'));
    for (const name of names.filter(name => /^(license|copying)(\.|$)/i.test(name))) {
      const file = path.join(folder, name);
      if (!(await stat(file)).isFile()) continue;
      licenses.push(`${pkg.name} ${pkg.version}\n${await readFile(file, 'utf8')}\n`);
    }
    if (names.includes('node_modules')) await collectLicenses(path.join(folder, 'node_modules'));
  }
};
await collectLicenses(path.join(source, 'node_modules'));
await writeFile(path.join(stage, 'licenses.txt'), licenses.sort().join('\n--------------------\n\n'), 'utf8');
assert.deepEqual(await inputs(), initial, 'Manual inputs changed during build; existing help was kept.');
const manifest = {
  version: (await readFile(path.join(root, 'VERSION'), 'utf8')).trim(),
  generator: 'VitePress 1.6.4', base: process.env.ANM_MANUAL_BASE || '/help/',
  inputs: initial,
  files: Object.fromEntries(await Promise.all((await files(stage)).map(async file => [path.relative(stage, file).split(path.sep).join('/'), digest(await readFile(file))]))),
};
await writeFile(path.join(stage, 'build.json'), JSON.stringify(manifest, null, 2) + '\n', 'utf8');
await check(stage);
if (process.env.ANM_MANUAL_BASE) {
  console.log('Online site ready at ' + stage);
} else {
  await rm(backup, { recursive: true, force: true });
  let moved = false;
  try { await rename(output, backup); moved = true; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  try { await rename(stage, output); } catch (error) { if (moved) await rename(backup, output); throw error; }
  await rm(backup, { recursive: true, force: true });
  console.log('Bundled offline help updated.');
}
