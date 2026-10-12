import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    outDir: 'benchmark-build',
    assetsDir: 'benchmark-assets',
    emptyOutDir: true,
    copyPublicDir: false,
    rollupOptions: { input: 'benchmark.html' },
  },
  worker: { format: 'es' },
});
