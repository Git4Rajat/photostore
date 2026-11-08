import fs from 'node:fs';
import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

// A second, small build of the shared-album page. The web app serves it at /static-album/; the cluster
// worker copies it to the storage static website (see backend/share_snapshot.py ensure_app), where the
// share snapshot pages load it. Relative base so it works under any path.
const listFiles = (): Plugin => ({
  name: 'static-album-files',
  closeBundle() {
    const dir = path.resolve(__dirname, 'build/static-album');
    const walk = (d: string): string[] => fs.readdirSync(d, { withFileTypes: true })
      .flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
    const files = walk(dir).map((f) => path.relative(dir, f).split(path.sep).join('/')).filter((f) => f !== 'app-files.json' && f !== 'static-album.html');
    const html = fs.readFileSync(path.join(dir, 'static-album.html'), 'utf8');
    const entry = /<script[^>]+src="\.?\/?([^"]+\.js)"/.exec(html)?.[1] || '';
    const css = [...html.matchAll(/<link[^>]+rel="stylesheet"[^>]+href="\.?\/?([^"]+\.css)"/g)].map((m) => m[1]);
    const hash = /-([A-Za-z0-9_]{6,})\.js$/.exec(entry)?.[1] || String(Date.now());
    fs.writeFileSync(path.join(dir, 'app-files.json'), JSON.stringify({ version: hash, entry, css, files }));
  },
});

export default defineConfig({
  base: './',
  publicDir: false,   // the share page needs none of the app's models/runtimes
  plugins: [react(), listFiles()],
  build: {
    outDir: 'build/static-album',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1000,
    rollupOptions: { input: path.resolve(__dirname, 'static-album.html') },
  },
});
