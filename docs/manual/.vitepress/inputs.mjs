import { createHash } from 'node:crypto';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = fileURLToPath(new URL('../../../', import.meta.url));
export const source = path.join(root, 'docs/manual');
export const output = path.join(root, 'src/animemachine/web/static/help');
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
export async function files(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'cache') continue;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...await files(full));
    else if (entry.isFile()) result.push(full);
  }
  return result.sort();
}
export async function inputs() {
  const selected = new Set((await files(source)).filter(file => /\.(md|mjs|js|css|json)$/.test(file)));
  selected.add(path.join(root, 'VERSION'));
  selected.add(path.join(root, 'deploy/compose/compose.yaml'));
  for (const file of [...selected].filter(file => file.endsWith('.md'))) {
    const text = await readFile(file, 'utf8');
    for (const match of text.matchAll(/!\[[^\]]*\]\(([^)\s]+)\)/g)) {
      const image = path.resolve(path.dirname(file), match[1]);
      if (!image.startsWith(path.join(root, 'docs/images') + path.sep)) throw Error('Unexpected manual image location');
      selected.add(image);
    }
  }
  return Object.fromEntries(await Promise.all([...selected].sort().map(async file => [path.relative(root, file).split(path.sep).join('/'), digest(await readFile(file))])));
}
