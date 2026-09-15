import { copyFileSync, existsSync, mkdirSync, readdirSync } from 'node:fs';
import path from 'node:path';

const cwd = process.cwd();
const sourceDir = path.join(cwd, 'node_modules', 'onnxruntime-web', 'dist');
const destinationDir = path.join(cwd, 'public', 'models', 'browser-ai', 'runtime');

if (!existsSync(sourceDir)) {
  throw new Error(`ONNX Runtime Web dist directory was not found at ${sourceDir}`);
}

mkdirSync(destinationDir, { recursive: true });

for (const name of readdirSync(sourceDir)) {
  if (!/^ort-wasm.*\.(wasm|mjs)$/.test(name)) {
    continue;
  }
  copyFileSync(path.join(sourceDir, name), path.join(destinationDir, name));
}
