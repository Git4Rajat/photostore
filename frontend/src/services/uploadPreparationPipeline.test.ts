import {Blob as NodeBlob} from 'node:buffer';

const mocks = vi.hoisted(() => ({prepare: vi.fn(), dispose: vi.fn(), fetch: vi.fn()}));
vi.mock('./uploadAssetPreparation', () => ({UploadAssetPreparer: class {
    prepare = mocks.prepare;
    dispose = mocks.dispose;
}}));
import {UploadPreparationPipeline} from './uploadPreparationPipeline';

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

test('bounded admission rejects excess jobs without decoding', async () => {
    mocks.prepare.mockImplementation((_file, {signal}) => new Promise(resolve => signal.addEventListener('abort',
        () => resolve({status: 'fallback'}), {once: true})));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 5}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal)).toMatchObject({preparationReport: {reason: 'busy'}});
    expect(mocks.prepare).toHaveBeenCalledTimes(5);
    pipeline.dispose();
    expect((await Promise.all(jobs)).map(({preparationReport, ...payload}) => payload)).toEqual(Array.from({length: 5}, () => ({})));
    expect(mocks.fetch).not.toHaveBeenCalled();
});

test('bounds PUT concurrency and abort drains waiting jobs', async () => {
    let active = 0, maximum = 0;
    mocks.fetch.mockImplementation((_url, {signal}) => new Promise((_resolve, reject) => {
        active++; maximum = Math.max(maximum, active);
        signal.addEventListener('abort', () => { active--; reject(new Error('aborted')); }, {once: true});
    }));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 5}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    await vi.advanceTimersByTimeAsync(45_000);
    expect(maximum).toBe(2);
    const results = await Promise.all(jobs);
    expect(results.map(({preparationReport, ...payload}) => payload)).toEqual(Array.from({length: 5}, () => ({})));
    expect(results.every(result => result.preparationReport?.reason === 'pipeline_timeout')).toBe(true);
    expect(results.every(result => result.preparationReport?.assets.preview.upload === 'aborted')).toBe(true);
    pipeline.dispose();
});

test('unsupported decoding and pre-aborted files keep original-only finalization', async () => {
    mocks.prepare.mockResolvedValue({status: 'fallback'});
    const pipeline = new UploadPreparationPipeline();
    const controller = new AbortController();
    const failed = await pipeline.start(source(), 'a'.repeat(64), targets, controller.signal);
    expect(failed.clientProcessing).toBeUndefined();
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

test('metadata success and network failure are separate outcomes', async () => {
    const descriptor = {version: 1, bytes: 6, sha256: 'b'.repeat(64), sourceSha256: 'a'.repeat(64)};
    vi.stubGlobal('Worker', class {
        onmessage?: (event: {data: unknown}) => void;
        terminate() {}
        postMessage() { queueMicrotask(() => this.onmessage?.({data: {status: 'generated', packet: {
            blob: new Blob(['packet'], {type: 'application/octet-stream'}), descriptor}}})); }
    });
    mocks.fetch.mockRejectedValueOnce(new Error('https://storage/?secret')).mockResolvedValue({ok: true, status: 201});
    const pipeline = new UploadPreparationPipeline();
    const result = await pipeline.start(source(), 'a'.repeat(64), {...targets, metadataBlobUrl: 'https://storage/metadata'}, new AbortController().signal);
    expect(result.clientMetadata).toEqual(descriptor);
    expect(result.clientProcessing?.preview).toBeUndefined();
    expect(result.preparationReport?.assets.preview).toMatchObject({generation: 'generated', upload: 'failed', uploadReason: 'network_error'});
    expect(result.preparationReport?.assets.metadata).toMatchObject({generation: 'generated', upload: 'uploaded', bytes: 6});
    expect(JSON.stringify(result.preparationReport)).not.toContain('secret');
    pipeline.dispose();
});
