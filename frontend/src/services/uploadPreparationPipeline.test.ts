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
    mocks.fetch.mockResolvedValue({ok: true});
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('only successful uploads are attached to finalization', async () => {
    const pipeline = new UploadPreparationPipeline();
    mocks.fetch.mockResolvedValueOnce({ok: true}).mockResolvedValueOnce({ok: false});
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal)).toEqual({
        clientProcessing: {preview: {alreadyUploaded: true, source: 'browser_upload'}}});
    expect(mocks.fetch.mock.calls[0][1].headers['x-ms-blob-type']).toBe('BlockBlob');
    pipeline.dispose();
});

test('bounded admission rejects excess jobs without decoding', async () => {
    mocks.prepare.mockImplementation((_file, {signal}) => new Promise(resolve => signal.addEventListener('abort',
        () => resolve({status: 'fallback'}), {once: true})));
    const pipeline = new UploadPreparationPipeline();
    const jobs = Array.from({length: 5}, () => pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal));
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, new AbortController().signal)).toEqual({});
    expect(mocks.prepare).toHaveBeenCalledTimes(5);
    pipeline.dispose();
    expect(await Promise.all(jobs)).toEqual(Array.from({length: 5}, () => ({})));
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
    expect(await Promise.all(jobs)).toEqual(Array.from({length: 5}, () => ({})));
    pipeline.dispose();
});

test('unsupported decoding and pre-aborted files keep original-only finalization', async () => {
    mocks.prepare.mockResolvedValue({status: 'fallback'});
    const pipeline = new UploadPreparationPipeline();
    const controller = new AbortController();
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, controller.signal)).toEqual({});
    controller.abort();
    expect(await pipeline.start(source(), 'a'.repeat(64), targets, controller.signal)).toEqual({});
    expect(mocks.prepare).toHaveBeenCalledTimes(1);
    pipeline.dispose();
});
