import { webcrypto } from 'node:crypto';
import { runCloudBenchmark } from './cloud-client';

const writes = vi.hoisted(() => [] as string[]);
vi.mock('@azure/storage-blob', () => ({ BlockBlobClient: class {
  url: string;
  constructor(url: string) { this.url = url; }
  async stageBlock() { writes.push(this.url + ':block'); }
  async commitBlockList() { writes.push(this.url + ':commit'); }
  async upload() { writes.push(this.url + ':put'); }
} }));

const file = (name: string) => ({ name, size: 3, type: '', lastModified: 123,
  arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
  slice: () => ({ size: 3 }),
});

let sensorFails = false;
let apiPayloads: Array<{ path: string; body: any }>;

beforeEach(() => {
  writes.length = 0; apiPayloads = []; sensorFails = false;
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('Worker', class {
    onmessage?: (event: { data: unknown }) => void;
    onerror?: () => void;
    postMessage() {
      queueMicrotask(() => this.onmessage?.({ data: sensorFails ? { status: 'failed' } : {
        status: 'generated', previewBlob: new Blob(['jpeg']), thumbnailBlob: new Blob(['thumb']),
        preview: { bytes: 4, width: 2048, height: 1200 }, thumbnail: { bytes: 5, width: 120, height: 70 },
        librawVersion: 'test-only', embeddedAllowed: false,
      } }));
    }
    terminate() {}
  });
  vi.stubGlobal('fetch', async (url: string, options: any) => {
    const body = JSON.parse(options.body); apiPayloads.push({ path: url, body });
    return { ok: true, status: 200, json: async () => ({ results: body.files.map((item: any, index: number) =>
      item.filename.endsWith('.ori') ? { index, error: 'Invalid filename' } : url.endsWith('init-batch') ? {
        index, blobName: `blob-${index}`, processingIsolated: true, uploadId: 'upload-1',
        blobUrl: 'https://test.blob.core.windows.net/images/blob',
        thumbnailBlobUrl: 'https://test.blob.core.windows.net/thumbnails/blob',
        previewBlobUrl: 'https://test.blob.core.windows.net/thumbnails/preview/blob.jpg',
      } : { index, complete: true, filename: item.filename, bytesReceived: item.totalSize, processingIsolated: true }) }) };
  });
});
afterEach(() => vi.unstubAllGlobals());

const config = (arm = 'derivatives') => ({ expectedFiles: 1, prefix: `rawbench-run-12345678-${arm}-`, runId: 'run-12345678',
  arm, workerUrl: '/worker.js', storageAccount: 'test', uploadBase: 'https://upload.example', token: 'test-only' });

test('derivatives use three distinct storage destinations and no base64 preview', async () => {
  const result = await runCloudBenchmark(config(), [file('camera.cr3')], () => {});
  expect(result.records[0].status).toBe('finalized');
  expect(writes.filter(value => value.endsWith(':put'))).toHaveLength(2);
  expect(writes.some(value => value.includes('/preview/'))).toBe(true);
  const final = apiPayloads.find(value => value.path.endsWith('finalize-batch'))!.body.files[0];
  expect(final.clientProcessing.preview.alreadyUploaded).toBe(true);
  expect(final.clientProcessing.preview.data).toBeUndefined();
  expect(final.benchmark).toEqual({ runId: 'run-12345678', arm: 'derivatives' });
}, 10000);

test('sensor failure still finalizes original, without fallback-triggering reports', async () => {
  sensorFails = true;
  const result = await runCloudBenchmark(config(), [file('cinema.ari')], () => {});
  expect(result.records[0].status).toBe('finalized');
  expect(writes.filter(value => value.endsWith(':put'))).toHaveLength(0);
  const final = apiPayloads.find(value => value.path.endsWith('finalize-batch'))!.body.files[0];
  expect(final.clientProcessingReport).toBeUndefined();
  expect(final.clientProcessing).toBeUndefined();
}, 10000);

test('unsupported extension is preserved and reports real API rejection', async () => {
  const result = await runCloudBenchmark(config('originals'), [file('camera.ori')], () => {});
  expect(result.records[0].filename).toMatch(/\.ori$/);
  expect(result.records[0].error).toContain('Invalid filename');
  expect(writes).toHaveLength(0);
}, 10000);
