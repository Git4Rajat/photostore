import {Blob as NodeBlob} from 'node:buffer';
import {UploadAssetPreparer} from './uploadAssetPreparation';
const video = vi.hoisted(() => ({prepare: vi.fn()}));
vi.mock('./videoThumbnailPreparation', () => ({prepareVideoThumbnail: video.prepare}));

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
    video.prepare.mockReset().mockResolvedValue({thumbnail: jpeg(), runtime: 'native_video'});
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test('with maxActive=1, serializes decoding and starts the next file after completion', async () => {
    const service = new UploadAssetPreparer(factory, 4, 30_000, 1);
    const first = service.prepare(new File(['raw'], 'a.CR3'));
    const second = service.prepare(new File(['jpeg'], 'b.jpg'));
    expect(factory).toHaveBeenCalledTimes(1);
    expect(factory).toHaveBeenNthCalledWith(1, true);
    expect(workers[0].postMessage).toHaveBeenCalledWith(expect.objectContaining({preserveEligibleJpeg: true}));
    complete(0);
    expect(factory).toHaveBeenNthCalledWith(2, false);
    complete(1);
    expect((await first).status).toBe('generated');
    expect((await second).status).toBe('generated');
    expect(workers.every(worker => vi.mocked(worker.terminate).mock.calls.length === 1)).toBe(true);
    service.dispose();
});

test('by default, runs more than one decoder concurrently instead of fully serializing', async () => {
    const service = new UploadAssetPreparer(factory);
    const first = service.prepare(new File(['raw'], 'a.CR3'));
    const second = service.prepare(new File(['raw'], 'b.CR3'));
    const third = service.prepare(new File(['raw'], 'c.CR3'));
    // First two dispatch immediately side by side; the third waits for a free slot.
    expect(factory).toHaveBeenCalledTimes(2);
    complete(0);
    expect(factory).toHaveBeenCalledTimes(3);
    complete(1);
    complete(2);
    expect((await first).status).toBe('generated');
    expect((await second).status).toBe('generated');
    expect((await third).status).toBe('generated');
    service.dispose();
});

test('retains explicit JPEG re-encode opt-out for local comparisons', async () => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['jpeg'], 'photo.jpg'), {preserveEligibleJpeg: false});
    expect(workers[0].postMessage).toHaveBeenCalledWith(expect.objectContaining({preserveEligibleJpeg: false}));
    complete(0);
    expect((await pending).status).toBe('generated');
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
    const service = new UploadAssetPreparer(factory, 1, 30_000, 1);
    const active = service.prepare(new File(['raw'], 'a.CR3'));
    const queued = service.prepare(new File(['raw'], 'b.CR3'));
    expect(await service.prepare(new File(['raw'], 'c.CR3'))).toEqual({status: 'fallback', reason: 'busy'});
    service.dispose();
    expect(await active).toEqual({status: 'fallback', reason: 'disposed'});
    expect(await queued).toEqual({status: 'fallback', reason: 'disposed'});
});

test('each decoder gets its own timeout after queue admission, retrying once before falling back', async () => {
    const service = new UploadAssetPreparer(factory, 4, 100, 1);
    const active = service.prepare(new File(['raw'], 'a.CR3'));
    const queued = service.prepare(new File(['raw'], 'b.CR3'));
    await vi.advanceTimersByTimeAsync(100);
    expect(factory).toHaveBeenCalledTimes(2); // first attempt + one retry, both still for a.CR3
    await vi.advanceTimersByTimeAsync(100);
    expect(await active).toEqual({status: 'fallback', reason: 'timeout', failureReason: 'generation_timeout'});
    expect(factory).toHaveBeenCalledTimes(3); // b.CR3 only starts once a.CR3's retry also times out
    complete(2);
    expect((await queued).status).toBe('generated');
    expect(workers.every(worker => vi.mocked(worker.terminate).mock.calls.length === 1)).toBe(true);
    service.dispose();
});

test('does not retry a worker-reported decode failure, only timeout/worker-error', async () => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['raw'], 'a.CR3'));
    complete(0, {status: 'failed', error: 'Unsupported file format or not RAW file'});
    expect(await pending).toMatchObject({status: 'fallback', reason: 'generation_failed', failureReason: 'unsupported_raw'});
    expect(factory).toHaveBeenCalledTimes(1);
    service.dispose();
});

test('retries once on a worker error before falling back', async () => {
    const service = new UploadAssetPreparer(factory);
    const pending = service.prepare(new File(['raw'], 'a.CR3'));
    workers[0].onerror?.call(workers[0], {} as ErrorEvent);
    expect(factory).toHaveBeenCalledTimes(2);
    complete(1);
    expect((await pending).status).toBe('generated');
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

test('prepares video thumbnail without creating an image decoder worker', async () => {
    const service = new UploadAssetPreparer(factory);
    expect(await service.prepare(new File(['video'], 'a.mp4'))).toEqual({status: 'generated', thumbnail: jpeg(), runtime: 'native_video'});
    expect(factory).not.toHaveBeenCalled();
    expect(video.prepare).toHaveBeenCalledOnce();
    service.dispose();
});

test('skips already-aborted work', async () => {
    const service = new UploadAssetPreparer(factory);
    const controller = new AbortController();
    controller.abort();
    expect(await service.prepare(new File(['raw'], 'a.CR3'), {signal: controller.signal})).toEqual({status: 'fallback', reason: 'aborted'});
    expect(factory).not.toHaveBeenCalled();
    service.dispose();
});
