import {Blob as NodeBlob} from 'node:buffer';
import {UploadAssetPreparer} from './uploadAssetPreparation';

const workers: Worker[] = [];
const factory = vi.fn(() => {
    const worker = {onmessage: null, onerror: null, postMessage: vi.fn(), terminate: vi.fn()} as unknown as Worker;
    workers.push(worker);
    return worker;
});
const jpeg = () => ({blob: new Blob(['jpeg'], {type: 'image/jpeg'}), width: 100, height: 50, quality: .9});
const complete = (index: number, data: unknown = {status: 'generated', preview: jpeg(), thumbnail: jpeg()}) => (
    workers[index].onmessage?.call(workers[index], {data} as MessageEvent)
);

beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('Blob', NodeBlob);
    workers.length = 0;
    factory.mockClear();
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('serializes decoding and starts the next file after completion', async () => {
    const service = new UploadAssetPreparer(factory);
    const first = service.prepare(new File(['raw'], 'a.CR3'));
    const second = service.prepare(new File(['jpeg'], 'b.jpg'));
    expect(factory).toHaveBeenCalledTimes(1);
    expect(factory).toHaveBeenNthCalledWith(1, true);
    complete(0);
    expect(factory).toHaveBeenNthCalledWith(2, false);
    complete(1);
    expect((await first).status).toBe('generated');
    expect((await second).status).toBe('generated');
    expect(workers.every(worker => vi.mocked(worker.terminate).mock.calls.length === 1)).toBe(true);
    service.dispose();
});

test.each(['heic', 'heif', 'hif', 'HIF'])('routes %s to the bounded HEIF worker', async extension => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['heif'], 'photo.' + extension));
    expect(factory).toHaveBeenCalledWith(false, true);
    expect(workers[0].postMessage).toHaveBeenCalledWith(expect.objectContaining({maxMegapixels: 120, maxBytes: 256 * 1024 * 1024}));
    complete(0);
    expect((await pending).status).toBe('generated');
    service.dispose();
});

test('returns busy fallback instead of growing an unbounded queue', async () => {
    const service = new UploadAssetPreparer(factory, 1);
    const active = service.prepare(new File(['raw'], 'a.CR3'));
    const queued = service.prepare(new File(['raw'], 'b.CR3'));
    expect(await service.prepare(new File(['raw'], 'c.CR3'))).toEqual({status: 'fallback', reason: 'busy'});
    service.dispose();
    expect(await active).toEqual({status: 'fallback', reason: 'disposed'});
    expect(await queued).toEqual({status: 'fallback', reason: 'disposed'});
});

test('each decoder gets its own timeout after queue admission', async () => {
    const service = new UploadAssetPreparer(factory, 4, 100);
    const active = service.prepare(new File(['raw'], 'a.CR3'));
    const queued = service.prepare(new File(['raw'], 'b.CR3'));
    await vi.advanceTimersByTimeAsync(100);
    expect(await active).toEqual({status: 'fallback', reason: 'timeout', failureReason: 'generation_timeout'});
    expect(factory).toHaveBeenCalledTimes(2);
    complete(1);
    expect((await queued).status).toBe('generated');
    expect(workers.every(worker => vi.mocked(worker.terminate).mock.calls.length === 1)).toBe(true);
    service.dispose();
});

test('abort cancels active work and frees the next job', async () => {
    const service = new UploadAssetPreparer(factory);
    const controller = new AbortController();
    const active = service.prepare(new File(['raw'], 'a.CR3'), {signal: controller.signal});
    const queued = service.prepare(new File(['raw'], 'b.CR3'));
    controller.abort();
    expect(await active).toEqual({status: 'fallback', reason: 'aborted'});
    expect(factory).toHaveBeenCalledTimes(2);
    complete(1);
    expect((await queued).status).toBe('generated');
    service.dispose();
});

test('handles worker startup and generation failures without throwing into upload lanes', async () => {
    const unsupported = new UploadAssetPreparer(() => { throw new Error('Worker unavailable'); });
    expect(await unsupported.prepare(new File(['raw'], 'a.CR3'))).toEqual({status: 'fallback', reason: 'unsupported', failureReason: 'worker_start_failed'});
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['raw'], 'a.CR3'));
    complete(0, {status: 'failed', error: 'Decoder unsupported'});
    expect(await pending).toMatchObject({status: 'fallback', reason: 'generation_failed', failureReason: 'generation_failed'});
    service.dispose();
});

test('rejects oversized or invalid worker outputs', async () => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['raw'], 'a.CR3'));
    complete(0, {status: 'generated', preview: {...jpeg(), width: 4096}, thumbnail: jpeg()});
    expect(await pending).toMatchObject({status: 'fallback', reason: 'generation_failed', failureReason: 'invalid_derivative'});
    service.dispose();
});

test('accepts the existing RAW worker blob/dimension message shape', async () => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['raw'], 'a.CR3'));
    const image = jpeg();
    complete(0, {status: 'generated', preview: {width: 100, height: 50}, previewBlob: image.blob, thumbnail: {width: 100, height: 50}, thumbnailBlob: image.blob});
    expect((await pending).status).toBe('generated');
    service.dispose();
});

test('skips video and already-aborted work', async () => {
    const service = new UploadAssetPreparer(factory);
    expect(await service.prepare(new File(['video'], 'a.mp4'))).toEqual({status: 'fallback', reason: 'unsupported'});
    const controller = new AbortController();
    controller.abort();
    expect(await service.prepare(new File(['raw'], 'a.CR3'), {signal: controller.signal})).toEqual({status: 'fallback', reason: 'aborted'});
    expect(factory).not.toHaveBeenCalled();
    service.dispose();
});
