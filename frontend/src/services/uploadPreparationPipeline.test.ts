import {Blob as NodeBlob} from 'node:buffer';

const mocks = vi.hoisted(() => ({prepare: vi.fn(), dispose: vi.fn(), fetch: vi.fn(), exif: vi.fn()}));
vi.mock('./uploadAssetPreparation', () => ({UploadAssetPreparer: class {
    prepare = mocks.prepare;
    dispose = mocks.dispose;
}}));
vi.mock('../utils/browserExifTool', () => ({extractBrowserExif: mocks.exif}));
import {UploadPreparationPipeline} from './uploadPreparationPipeline';

const browserVerdict = {ok: true, result: {source: 'browser_exiftool', verifiedComplete: true, exifToolJson: {'EXIF:Make': 'Canon'}}};

const source = () => new File(['source'], 'photo.cr3');
const targets = {previewBlobUrl: 'https://storage/preview', thumbnailBlobUrl: 'https://storage/thumb'};
const assets = () => ({status: 'generated', preview: {blob: new Blob(['preview'], {type: 'image/jpeg'})},
    thumbnail: {blob: new Blob(['thumb'], {type: 'image/jpeg'})}});

beforeEach(() => {
    vi.useFakeTimers(); vi.clearAllMocks();
    vi.stubGlobal('Blob', NodeBlob);
    vi.stubGlobal('fetch', mocks.fetch);
    mocks.prepare.mockResolvedValue(assets());
    mocks.fetch.mockResolvedValue({ok: true, status: 201});
    mocks.exif.mockResolvedValue(browserVerdict);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('only successful uploads are attached to finalization', async () => {
    const pipeline = new UploadPreparationPipeline();
    mocks.fetch.mockResolvedValueOnce({ok: true, status: 201}).mockResolvedValueOnce({ok: false, status: 403});
    const result = await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    expect(result).toMatchObject({
        clientProcessing: {preview: {alreadyUploaded: true, source: 'browser_upload'}}});
    expect(result.clientProcessing?.thumbnail).toBeUndefined();
    expect(result.preparationReport?.assets.preview).toMatchObject({generation: 'generated', upload: 'uploaded', httpStatus: 201});
    expect(result.preparationReport?.assets.thumbnail).toMatchObject({generation: 'generated', upload: 'failed', uploadReason: 'http_error', httpStatus: 403});
    expect(result.preparationReport?.assets.metadata).toMatchObject({generation: 'skipped', generationReason: 'missing_target'});
    expect(mocks.fetch.mock.calls[0][1].headers['x-ms-blob-type']).toBe('BlockBlob');
    pipeline.dispose();
});

test('asset PUTs are marked high priority so they are not stuck behind slower chunk PUTs sharing the same origin', async () => {
    const pipeline = new UploadPreparationPipeline();
    await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    expect(mocks.fetch.mock.calls.length).toBeGreaterThan(0);
    for (const call of mocks.fetch.mock.calls) {
        expect(call[1].priority).toBe('high');
    }
    pipeline.dispose();
});

test.each(['IMG_0127.HEIC', 'photo.jpg', 'sample_001.cr3', 'sample_001.rw2', 'sample_001.3fr', 'sample_001.dng'])('browser EXIF runs for %s and attaches the verdict', async name => {
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(new File(['x'], name), 'a'.repeat(64), targets, new AbortController().signal);
    expect(mocks.exif).toHaveBeenCalledTimes(1);
    expect(result.clientProcessing?.exif).toEqual(browserVerdict.result);
    expect(result.clientProcessingReport).toEqual([expect.objectContaining({step: 'exif', status: 'done', reason: 'done', runtime: 'browser-exiftool-wasm'})]);
    pipeline.dispose();
});

test('browser EXIF runs on a file above the metadata packet size without changing its upload', async () => {
    const big = new File([new Uint8Array(300 * 1024 * 1024)], 'sample_001.raf');
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(big, 'a'.repeat(64), targets, new AbortController().signal);
    expect(mocks.exif).toHaveBeenCalledTimes(1);
    expect(result.clientProcessing?.exif).toEqual(browserVerdict.result);
    pipeline.dispose();
});

test.each([
    ['timeout', 'failed', 'unknown_error'],
    ['parse_error', 'failed', 'unknown_error'],
    ['unsupported_runtime', 'unsupported', 'unsupported_runtime'],
    ['tool_unavailable', 'unsupported', 'unsupported_runtime'],
])('browser EXIF failure %s reports %s so the server fallback runs', async (reason, status, reportReason) => {
    mocks.exif.mockResolvedValue({ok: false, reason, ms: 12});
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(new File(['x'], 'sample_001.cr3'), 'a'.repeat(64), targets, new AbortController().signal);
    expect(result.clientProcessing?.exif).toBeUndefined();
    expect(result.clientProcessingReport).toEqual([expect.objectContaining({
        step: 'exif', status, reason: reportReason, browserExifReason: reason, browserExifMs: 12,
    })]);
    pipeline.dispose();
});

test('a file read failure reports browser EXIF failed so the server fallback runs', async () => {
    const file = new File(['x'], 'sample_001.cr3');
    vi.spyOn(file, 'arrayBuffer').mockRejectedValue(new DOMException('read failed', 'NotReadableError'));
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(file, 'a'.repeat(64), targets, new AbortController().signal);
    expect(mocks.exif).not.toHaveBeenCalled();
    expect(result.clientProcessing?.exif).toBeUndefined();
    expect(result.clientProcessingReport).toEqual([expect.objectContaining({
        step: 'exif', status: 'failed', reason: 'unknown_error', browserExifReason: 'read_error',
    })]);
    pipeline.dispose();
});

test('browser EXIF is not run for videos', async () => {
    metadataWorker();
    mocks.prepare.mockResolvedValue({status: 'generated', thumbnail: assets().thumbnail, runtime: 'ffmpeg_wasm'});
    const pipeline = new UploadPreparationPipeline();
    await pipeline.start(new File(['video'], 'clip.mkv'), 'a'.repeat(64), {thumbnailBlobUrl: 'https://storage/thumb'}, new AbortController().signal);
    expect(mocks.exif).not.toHaveBeenCalled();
    pipeline.dispose();
});

test('reports preview byte reuse without changing finalization trust or upload handling', async () => {
    const prepared = assets();
    mocks.prepare.mockResolvedValue({...prepared, preview: {...prepared.preview, passthrough: true}});
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    expect(result.preparationReport?.assets.preview.passthrough).toBe(true);
    expect(result.preparationReport?.assets.thumbnail.passthrough).toBeUndefined();
    expect(result.clientProcessing?.preview).toEqual({alreadyUploaded: true, source: 'browser_upload'});
    pipeline.dispose();
});

test.each(['ffmpeg_wasm', 'embedded_video_jpeg'])('video prepares %s thumbnail and metadata without uploading an image preview', async runtime => {
    metadataWorker();
    mocks.prepare.mockResolvedValue({status: 'generated', thumbnail: assets().thumbnail, runtime, readBytes: 12000, readCalls: 14});
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(new File(['video'], 'clip.mkv'), 'a'.repeat(64), {
        thumbnailBlobUrl: 'https://storage/thumb', metadataBlobUrl: 'https://storage/metadata', uploadPreparationEnabled: true,
    }, new AbortController().signal);
    expect(result.clientProcessing?.thumbnail.alreadyUploaded).toBe(true);
    expect(result.clientProcessing?.preview).toBeUndefined();
    expect(result.clientMetadata).toBeDefined();
    expect(result.preparationReport?.assets.thumbnail.runtime).toBe(runtime);
    expect(result.preparationReport?.assets.thumbnail).toMatchObject({readBytes: 12000, readCalls: 14});
    expect(result.preparationReport?.assets.preview.generation).toBe('skipped');
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    pipeline.dispose();
});

test('bounded admission queues excess files until uploads release a job slot', async () => {
    mocks.fetch.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve({ok: true, status: 201}), 1000)));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 5}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    const sixth = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.prepare).toHaveBeenCalledTimes(5);
    await vi.advanceTimersByTimeAsync(10_000);
    const results = await Promise.all([...jobs, sixth]);
    expect(mocks.prepare).toHaveBeenCalledTimes(6);
    expect(results.every(result => result.clientProcessing?.preview && result.clientProcessing?.thumbnail)).toBe(true);
    expect(results[5].preparationReport?.admissionMs).toBeGreaterThanOrEqual(1000);
    expect(results.every(result => result.preparationReport?.reason !== 'busy')).toBe(true);
    pipeline.dispose();
});

test('all 104 queued files deliver three assets while keeping the PUT cap at two', async () => {
    metadataWorker();
    let active = 0, maximum = 0;
    mocks.fetch.mockImplementation(() => new Promise(resolve => {
        active++; maximum = Math.max(maximum, active);
        setTimeout(() => { active--; resolve({ok: true, status: 201}); }, 20);
    }));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 104}, () => pipeline.start(source(), 'a'.repeat(64),
        {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal));
    await vi.advanceTimersByTimeAsync(10_000);
    const results = await Promise.all(jobs);
    expect(maximum).toBe(2);
    expect(mocks.prepare).toHaveBeenCalledTimes(104);
    expect(mocks.fetch).toHaveBeenCalledTimes(312);
    expect(results.every(result => result.clientProcessing?.preview && result.clientProcessing?.thumbnail && result.clientMetadata)).toBe(true);
    expect(results.every(result => result.preparationReport?.reason !== 'busy')).toBe(true);
    pipeline.dispose();
});

test('bounds PUT concurrency and abort drains waiting jobs', async () => {
    let active = 0, maximum = 0;
    mocks.fetch.mockImplementation((_url, {signal}) => new Promise((_resolve, reject) => {
        active++; maximum = Math.max(maximum, active);
        signal.addEventListener('abort', () => { active--; reject(new Error('aborted')); }, {once: true});
    }));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 5}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    await vi.advanceTimersByTimeAsync(240_000);
    expect(maximum).toBe(2);
    const results = await Promise.all(jobs);
    expect(results.every(result => !result.clientProcessing?.preview && !result.clientProcessing?.thumbnail)).toBe(true);
    expect(results.every(result => result.preparationReport?.assets.preview.upload === 'failed')).toBe(true);
    expect(results.every(result => result.preparationReport?.assets.preview.uploadReason === 'upload_timeout')).toBe(true);
    expect(results.every(result => result.preparationReport?.assets.thumbnail.uploadReason === 'upload_timeout')).toBe(true);
    pipeline.dispose();
});

test('unsupported decoding and pre-aborted files keep original-only finalization', async () => {
    mocks.prepare.mockResolvedValue({status: 'fallback'});
    const pipeline = new UploadPreparationPipeline();
    const controller = new AbortController();
    const failed = await pipeline.start(source(), 'a'.repeat(64), targets, controller.signal);
    expect(failed.clientProcessing?.preview).toBeUndefined();
    expect(failed.clientProcessing?.thumbnail).toBeUndefined();
    expect(failed.preparationReport?.assets.preview.generation).toBe('failed');
    controller.abort();
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, controller.signal)).toMatchObject({preparationReport: {reason: 'caller_aborted'}});
    expect(mocks.prepare).toHaveBeenCalledTimes(1);
    pipeline.dispose();
});

test('backend capability and format gates are recorded without starting workers', async () => {
    const pipeline = new UploadPreparationPipeline();
    const disabled = await pipeline.start(source(), 'a'.repeat(64), {...targets, uploadPreparationEnabled: false}, new AbortController().signal);
    expect(disabled.preparationReport?.reason).toBe('backend_disabled');
    expect(mocks.prepare).not.toHaveBeenCalled();
    const result = await pipeline.start(source(), 'a'.repeat(64), {...targets, metadataPreparationReason: 'unqualified_family'}, new AbortController().signal);
    expect(result.preparationReport?.assets.metadata).toMatchObject({target: false, generation: 'skipped', generationReason: 'unqualified_family'});
    pipeline.dispose();
});

test.each([
    [{status: 'generated', packet: {}}, 'packet_invalid'],
    [{status: 'fallback', reason: 'metadata_budget'}, 'metadata_budget'],
])('metadata failures remain distinguishable from successful derivatives', async (data, reason) => {
    const terminate = vi.fn();
    vi.stubGlobal('Worker', class {
        onmessage?: (event: {data: unknown}) => void;
        terminate = terminate;
        postMessage() { queueMicrotask(() => this.onmessage?.({data})); }
    });
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    expect(result.clientProcessing?.preview.alreadyUploaded).toBe(true);
    expect(result.clientMetadata).toBeUndefined();
    expect(result.preparationReport?.assets.metadata).toMatchObject({generation: 'failed', generationReason: reason, upload: 'not_attempted'});
    expect(terminate).toHaveBeenCalledOnce();
    pipeline.dispose();
});

test('network retries preserve successful metadata and derivatives', async () => {
    const descriptor = {version: 1, bytes: 6, sha256: 'b'.repeat(64), sourceSha256: 'a'.repeat(64)};
    vi.stubGlobal('Worker', class {
        onmessage?: (event: {data: unknown}) => void;
        terminate() {}
        postMessage() { queueMicrotask(() => this.onmessage?.({data: {status: 'generated', packet: {
            blob: new Blob(['packet'], {type: 'application/octet-stream'}), descriptor}}})); }
    });
    let previewAttempts = 0;
    mocks.fetch.mockImplementation(async url => {
        if (url === targets.previewBlobUrl && ++previewAttempts === 1) throw new Error('https://storage/?secret');
        return {ok: true, status: 201};
    });
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(1000);
    const result = await pending;
    expect(result.clientMetadata).toEqual(descriptor);
    expect(result.clientProcessing?.preview.alreadyUploaded).toBe(true);
    expect(result.preparationReport?.assets.preview).toMatchObject({generation: 'generated', upload: 'uploaded', attempts: 2});
    expect(result.preparationReport?.assets.preview.uploadReason).toBeUndefined();
    expect(result.preparationReport?.assets.metadata).toMatchObject({generation: 'generated', upload: 'uploaded', bytes: 6});
    expect(JSON.stringify(result.preparationReport)).not.toContain('secret');
    pipeline.dispose();
});

function metadataWorker(respond = true) {
    vi.stubGlobal('Worker', class {
        onmessage?: (event: {data: unknown}) => void;
        terminate() {}
        postMessage() {
            if (respond) queueMicrotask(() => this.onmessage?.({data: {status: 'generated', packet: {
                blob: new Blob(['packet'], {type: 'application/octet-stream'}), descriptor: {version: 1}}}}));
        }
    });
}

test('uploads ready metadata during decoding and gives derivatives independent upload deadlines', async () => {
    metadataWorker();
    mocks.prepare.mockImplementation(() => new Promise(resolve => setTimeout(() => resolve(assets()), 30_000)));
    mocks.fetch.mockImplementation(url => url.endsWith('/metadata') ? Promise.resolve({ok: true, status: 201})
        : new Promise(resolve => setTimeout(() => resolve({ok: true, status: 201}), 25_000)));
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(1000);
    expect(mocks.fetch.mock.calls.map(([url]) => url)).toEqual(['https://storage/metadata']);
    await vi.advanceTimersByTimeAsync(54_000);
    const result = await pending;
    expect(result.clientMetadata).toBeDefined();
    expect(result.clientProcessing?.preview.alreadyUploaded).toBe(true);
    expect(result.clientProcessing?.thumbnail.alreadyUploaded).toBe(true);
    pipeline.dispose();
});

test('metadata generation timeout does not cancel successful derivatives', async () => {
    metadataWorker(false);
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(30_000);
    const result = await pending;
    expect(result.clientProcessing?.preview).toBeDefined();
    expect(result.clientProcessing?.thumbnail).toBeDefined();
    expect(result.preparationReport?.assets.metadata.generationReason).toBe('generation_timeout');
    pipeline.dispose();
});

test('an unexpected decoder error does not discard uploaded metadata', async () => {
    metadataWorker();
    mocks.prepare.mockRejectedValue(new Error('unexpected decoder failure'));
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    expect(result.clientMetadata).toBeDefined();
    expect(result.preparationReport?.reason).toBe('internal_error');
    pipeline.dispose();
});

test.each([408, 429, 500, 502, 503, 504])('retries transient HTTP %i failures within the asset budget', async status => {
    let attempts = 0;
    mocks.fetch.mockImplementation(async url => url === targets.previewBlobUrl && ++attempts < 3
        ? {ok: false, status} : {ok: true, status: 201});
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(2000);
    const result = await pending;
    expect(result.preparationReport?.assets.preview).toMatchObject({upload: 'uploaded', attempts: 3, httpStatus: 201});
    expect(result.clientProcessing?.preview).toBeDefined();
    pipeline.dispose();
});

test('exhausted transient retries affect only the failing asset', async () => {
    mocks.fetch.mockImplementation(async url => url === targets.previewBlobUrl ? {ok: false, status: 503} : {ok: true, status: 201});
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(2000);
    const result = await pending;
    expect(result.clientProcessing?.preview).toBeUndefined();
    expect(result.clientProcessing?.thumbnail).toBeDefined();
    expect(result.preparationReport?.assets.preview).toMatchObject({upload: 'failed', attempts: 3, httpStatus: 503});
    pipeline.dispose();
});

test('honors Retry-After without waiting past the asset upload budget', async () => {
    mocks.fetch.mockImplementation(async url => url === targets.previewBlobUrl
        ? {ok: false, status: 429, headers: new Headers({'Retry-After': '120'})} : {ok: true, status: 201});
    const pipeline = new UploadPreparationPipeline();
    const pending = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(45_000);
    const result = await pending;
    expect(result.preparationReport?.assets.preview).toMatchObject({upload: 'failed', uploadReason: 'upload_timeout', attempts: 1});
    expect(result.clientProcessing?.thumbnail).toBeDefined();
    pipeline.dispose();
});

test('cancellation removes queued PUTs immediately without canceling other files', async () => {
    mocks.fetch.mockImplementation((_url, {signal}) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
    }));
    const pipeline = new UploadPreparationPipeline();
    const active = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    const controller = new AbortController();
    const queued = pipeline.start(source(), 'a'.repeat(64), targets, controller.signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    controller.abort();
    expect((await queued).preparationReport?.reason).toBe('caller_aborted');
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    pipeline.dispose(); await active;
});

test('cancellation during retry backoff prevents further attempts', async () => {
    mocks.fetch.mockResolvedValue({ok: false, status: 503});
    const pipeline = new UploadPreparationPipeline();
    const controller = new AbortController();
    const pending = pipeline.start(source(), 'a'.repeat(64), targets, controller.signal);
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    const result = await pending;
    await vi.advanceTimersByTimeAsync(2000);
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
    expect(result.preparationReport?.assets.preview).toMatchObject({upload: 'aborted', attempts: 1, uploadReason: 'caller_aborted'});
    pipeline.dispose();
});

test('dispose drains active and admission-queued files without starting queued decoders', async () => {
    mocks.prepare.mockImplementation((_file, {signal}) => new Promise(resolve => signal.addEventListener('abort',
        () => resolve({status: 'fallback', reason: 'aborted'}), {once: true})));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 100}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.prepare).toHaveBeenCalledTimes(5);
    pipeline.dispose();
    const results = await Promise.all(jobs);
    expect(results.every(result => result.preparationReport?.reason === 'disposed')).toBe(true);
    expect(mocks.prepare).toHaveBeenCalledTimes(5);
    expect(mocks.fetch).not.toHaveBeenCalled();
});

test('metadata takes the next PUT slot ahead of queued derivatives', async () => {
    metadataWorker();
    const resolveFetch: Array<() => void> = [];
    mocks.fetch.mockImplementation((_url, {signal}) => new Promise((resolve, reject) => {
        resolveFetch.push(() => resolve({ok: true, status: 201}));
        signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
    }));
    const pipeline = new UploadPreparationPipeline();
    const first = pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(0);
    const second = pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(0);
    resolveFetch[0]();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.fetch.mock.calls[2][0]).toBe('https://storage/metadata');
    pipeline.dispose(); await Promise.all([first, second]);
});
