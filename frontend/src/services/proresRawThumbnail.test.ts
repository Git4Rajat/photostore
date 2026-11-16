import {proresRawThumbnail} from './proresRawThumbnail';
const encode = vi.hoisted(() => vi.fn());
vi.mock('../utils/uploadDerivatives', () => ({generateUploadThumbnail: encode}));
let worker: {postMessage: ReturnType<typeof vi.fn>; terminate: ReturnType<typeof vi.fn>; onmessage?: (event: {data: unknown}) => void; onerror?: () => void};
let close: ReturnType<typeof vi.fn>;
beforeEach(() => {
    close = vi.fn();
    worker = {postMessage: vi.fn(), terminate: vi.fn()};
    vi.stubGlobal('Worker', vi.fn(function () { return worker; }));
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue({width: 120, height: 63, close}));
    encode.mockResolvedValue({width: 120, height: 63, blob: new Blob(['jpeg'], {type: 'image/jpeg'})});
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
const generated = () => ({status: 'generated', bytes: new Uint8Array([255, 216, 255]), readBytes: 6000, readCalls: 5});

test('mounts File in a lazy module worker, validates output and reports read counters', async () => {
    const file = new File(['video'], 'raw.mov');
    const result = proresRawThumbnail(file, new AbortController().signal);
    expect(worker.postMessage).toHaveBeenCalledWith({file});
    worker.onmessage!({data: generated()});
    expect(await result).toMatchObject({width: 120, readBytes: 6000, readCalls: 5});
    expect(worker.terminate).toHaveBeenCalledOnce(); expect(close).toHaveBeenCalledOnce();
});
test('pre-aborted work never creates a worker', async () => {
    const controller = new AbortController(); controller.abort();
    await expect(proresRawThumbnail(new File([], 'raw.mov'), controller.signal)).rejects.toThrow('aborted');
    expect(Worker).not.toHaveBeenCalled();
});
test('abort terminates an active decoder', async () => {
    const controller = new AbortController();
    const result = proresRawThumbnail(new File(['video'], 'raw.mov'), controller.signal);
    controller.abort();
    await expect(result).rejects.toThrow('aborted'); expect(worker.terminate).toHaveBeenCalledOnce();
});
test('timeout terminates an unresponsive decoder', async () => {
    vi.useFakeTimers();
    const result = proresRawThumbnail(new File(['video'], 'raw.mov'), new AbortController().signal);
    const rejected = expect(result).rejects.toThrow('timeout');
    await vi.advanceTimersByTimeAsync(30000);
    await rejected; expect(worker.terminate).toHaveBeenCalledOnce();
});
test.each([{status: 'failed', error: 'Video resource guard'}, {...generated(), readBytes: 65 * 1024 * 1024},
    {...generated(), bytes: new Uint8Array(512001)}, {...generated(), readCalls: -1}])('rejects worker failure/invalid bounded output', async data => {
    const result = proresRawThumbnail(new File(['video'], 'raw.mov'), new AbortController().signal);
    worker.onmessage!({data});
    await expect(result).rejects.toThrow(); expect(worker.terminate).toHaveBeenCalledOnce();
});
