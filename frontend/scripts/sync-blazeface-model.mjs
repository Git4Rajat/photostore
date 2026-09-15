import { cpSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const cwd = process.cwd();
const sourceDir = path.join(cwd, 'public', 'models', 'blazeface');
const destinationDir = path.join(cwd, 'public', 'models', 'browser-ai', 'models', 'blazeface');

if (!statSync(sourceDir, { throwIfNoEntry: false })) {
  throw new Error(`BlazeFace source model directory was not found at ${sourceDir}`);
}

mkdirSync(destinationDir, { recursive: true });

for (const entry of readdirSync(sourceDir, { withFileTypes: true })) {
  if (entry.name.toLowerCase() === 'readme.md') {
    continue;
  }
  const from = path.join(sourceDir, entry.name);
  const to = path.join(destinationDir, entry.name);
  if (entry.isDirectory()) {
    cpSync(from, to, { recursive: true, force: true });
  } else {
    cpSync(from, to, { force: true });
  }
}
