import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const projectRoot = path.dirname(fileURLToPath(import.meta.url));
const tfjsCoreEntry = path.resolve(projectRoot, 'node_modules/@tensorflow/tfjs-core/dist/index.js');
const tfjsConverterEntry = path.resolve(projectRoot, 'node_modules/@tensorflow/tfjs-converter/dist/index.js');
const tfjsBackendCpuEntry = path.resolve(projectRoot, 'node_modules/@tensorflow/tfjs-backend-cpu/dist/index.js');
const tfjsBackendWebglEntry = path.resolve(projectRoot, 'node_modules/@tensorflow/tfjs-backend-webgl/dist/index.js');

export default defineConfig({
  plugins: [react()],
  resolve: {
    dedupe: [
      '@tensorflow/tfjs-core',
      '@tensorflow/tfjs-converter',
      '@tensorflow/tfjs-backend-cpu',
      '@tensorflow/tfjs-backend-webgl',
    ],
    alias: [
      {
        find: 'jszip',
        replacement: 'jszip/dist/jszip.min.js',
      },
      {
        find: /^@tensorflow\/tfjs-core$/,
        replacement: tfjsCoreEntry,
      },
      {
        find: /^@tensorflow\/tfjs-converter$/,
        replacement: tfjsConverterEntry,
      },
      {
        find: /^@tensorflow\/tfjs-backend-cpu$/,
        replacement: tfjsBackendCpuEntry,
      },
      {
        find: /^@tensorflow\/tfjs-backend-webgl$/,
        replacement: tfjsBackendWebglEntry,
      },
    ],
  },
  build: {
    outDir: 'build',
    emptyOutDir: true,
    // Increase chunk warning threshold (KB) and add manual chunking
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('react') || id.includes('react-dom') || id.includes('@remix-run') || id.includes('@reduxjs')) {
              return 'vendor.react';
            }
            if (id.includes('lodash')) {
              return 'vendor.lodash';
            }
            if (id.includes('chart.js') || id.includes('d3')) {
              return 'vendor.charts';
            }
            return 'vendor';
          }
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    isolate: true,
    // Disable worker threads to avoid fork/worker timeouts in constrained CI/dev environments
    threads: false,
    // Increase default test timeout (ms) to allow slower startup in CI
    testTimeout: 120000,
  },
  server: {
    port: 3000,
  },
});
