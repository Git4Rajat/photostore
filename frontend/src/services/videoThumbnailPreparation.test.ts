const mocks = vi.hoisted(() => ({load: vi.fn(), probe: vi.fn(), read: vi.fn(), exec: vi.fn(), mount: vi.fn(),
    terminate: vi.fn(), bitmap: vi.fn(), encode: vi.fn(), createDir: vi.fn(), embedded: vi.fn()}));
vi.mock('../utils/videoEmbeddedThumbnail', () => ({embeddedVideoJpegs: mocks.embedded}));
vi.mock('@ffmpeg/ffmpeg', () => ({FFmpeg: class {
    load = mocks.load; ffprobe = mocks.probe; readFile = mocks.read; exec = mocks.exec;
    mount = mocks.mount; terminate = mocks.terminate; createDir = mocks.createDir;
}}));
vi.mock('../utils/uploadDerivatives', async () => ({...await vi.importActual('../utils/uploadDerivatives'), generateUploadThumbnail: mocks.encode}));
import {prepareVideoThumbnail, wasmVideoThumbnail, VIDEO_MAX_FRAME_PIXELS} from './videoThumbnailPreparation';

beforeEach(() => {
    vi.clearAllMocks();
    mocks.load.mockResolvedValue(true); mocks.probe.mockResolvedValue(-1); mocks.mount.mockResolvedValue(true);
    mocks.exec.mockResolvedValue(0);
    mocks.read.mockImplementation(path => path.endsWith('.json') ? JSON.stringify({streams: [{width: 1920, height: 1080}]}) : new Uint8Array([255, 216, 255]));
    mocks.bitmap.mockResolvedValue({width: 160, height: 120, close: vi.fn()}); vi.stubGlobal('createImageBitmap', mocks.bitmap);
    mocks.embedded.mockImplementation(async function* () {});
    mocks.encode.mockResolvedValue({blob: new Blob(['jpeg'], {type: 'image/jpeg'}), width: 120, height: 68});
});
afterEach(() => vi.unstubAllGlobals());

test('reads File through WORKERFS and accepts valid output for pinned ffprobe -1 quirk', async () => {
    const file = new File(['video'], 'clip.mkv');
    const result = await wasmVideoThumbnail(file, new AbortController().signal);
    expect(result.width).toBe(120);
    expect(mocks.mount).toHaveBeenCalledWith('WORKERFS', {files: [expect.any(File)]}, '/input');
    expect(mocks.load).toHaveBeenCalledWith(expect.objectContaining({wasmURL: expect.stringContaining('/video-runtime/ffmpeg-core-0.12.10.wasm')}));
    expect(mocks.exec.mock.calls[0][0]).toContain('1');
    const args = mocks.exec.mock.calls[0][0];
    expect(args[args.indexOf('-vf') + 1]).toContain('gte(dar,1)');
    expect(args[args.indexOf('-vf') + 1]).toContain('setsar=1');
    expect(mocks.terminate).toHaveBeenCalledOnce();
});

test.each([1, 2])('does not exempt ffprobe timeout or error %s', async code => {
    mocks.probe.mockResolvedValue(code);
    await expect(wasmVideoThumbnail(new File(['video'], 'clip.mov'), new AbortController().signal)).rejects.toThrow('unsupported');
    expect(mocks.exec).not.toHaveBeenCalled(); expect(mocks.terminate).toHaveBeenCalledOnce();
});

test.each([{}, {streams: []}, {streams: [{width: NaN, height: 100}]}])('reports unrecognized dimensions as unsupported decoding, not an oversized frame', async dimensions => {
    mocks.read.mockResolvedValue(JSON.stringify(dimensions));
    await expect(wasmVideoThumbnail(new File(['video'], 'clip.mov'), new AbortController().signal)).rejects.toThrow('unsupported');
    expect(mocks.exec).not.toHaveBeenCalled(); expect(mocks.terminate).toHaveBeenCalledOnce();
});

test('rejects recognized oversized frames before decoding', async () => {
    mocks.read.mockResolvedValue(JSON.stringify({streams: [{width: VIDEO_MAX_FRAME_PIXELS, height: 2}]}));
    await expect(wasmVideoThumbnail(new File(['video'], 'clip.mov'), new AbortController().signal)).rejects.toThrow('resource guard');
    expect(mocks.exec).not.toHaveBeenCalled(); expect(mocks.terminate).toHaveBeenCalledOnce();
});

test('retries frame zero for sub-second clips with no frame at one second', async () => {
    mocks.read.mockImplementation(path => path.endsWith('.json') ? JSON.stringify({streams: [{width: 320, height: 240}]})
        : mocks.exec.mock.calls.length === 1 ? new Uint8Array() : new Uint8Array([255, 216, 255]));
    await wasmVideoThumbnail(new File(['video'], 'short.mp4'), new AbortController().signal);
    expect(mocks.exec).toHaveBeenCalledTimes(2);
    expect(mocks.exec.mock.calls[1][0]).toContain('0');
});

test('retries frame zero when a successful seek creates no output file', async () => {
    mocks.read.mockImplementation(path => {
        if (path.endsWith('.json')) return JSON.stringify({streams: [{width: 320, height: 240}]});
        if (mocks.exec.mock.calls.length === 1) throw new Error('FS error');
        return new Uint8Array([255, 216, 255]);
    });
    await wasmVideoThumbnail(new File(['video'], 'short.mp4'), new AbortController().signal);
    expect(mocks.exec).toHaveBeenCalledTimes(2);
});

test('pre-aborted work never starts the WASM runtime', async () => {
    const controller = new AbortController(); controller.abort();
    await expect(wasmVideoThumbnail(new File(['video'], 'clip.mov'), controller.signal)).rejects.toThrow('aborted');
    expect(mocks.load).not.toHaveBeenCalled();
});

test('uses embedded JPEG before loading either video decoder and preserves read counters', async () => {
    mocks.embedded.mockImplementation(async function* () {
        yield {blob: new Blob(['jpeg']), width: 160, height: 120, readBytes: 8000, readCalls: 12};
    });
    expect(await prepareVideoThumbnail(new File(['video'], 'clip.crm'), new AbortController().signal)).toMatchObject({
        runtime: 'embedded_video_jpeg', thumbnail: {width: 120}, readBytes: 8000, readCalls: 12,
    });
    expect(mocks.load).not.toHaveBeenCalled(); expect(mocks.probe).not.toHaveBeenCalled();
    expect((await mocks.bitmap.mock.results[0].value).close).toHaveBeenCalledOnce();
});

test('tries another embedded image after an undecodable thumbnail', async () => {
    mocks.embedded.mockImplementation(async function* () {
        yield {blob: new Blob(['bad']), readBytes: 100, readCalls: 5};
        yield {blob: new Blob(['good']), readBytes: 1000, readCalls: 8};
    });
    mocks.bitmap.mockRejectedValueOnce(new Error('Invalid JPEG'));
    expect(await prepareVideoThumbnail(new File(['video'], 'clip.nev'), new AbortController().signal)).toMatchObject({
        runtime: 'embedded_video_jpeg', readBytes: 1000, readCalls: 8,
    });
    expect(mocks.load).not.toHaveBeenCalled(); expect(mocks.bitmap).toHaveBeenCalledTimes(2);
});

test('does not switch decoders after cancellation during embedded decode', async () => {
    const controller = new AbortController(), close = vi.fn();
    mocks.embedded.mockImplementation(async function* () { yield {blob: new Blob(['jpeg'])}; });
    mocks.bitmap.mockImplementationOnce(async () => { controller.abort(); return {width: 160, height: 120, close}; });
    await expect(prepareVideoThumbnail(new File(['video'], 'clip.crm'), controller.signal)).rejects.toThrow('aborted');
    expect(close).toHaveBeenCalledOnce(); expect(mocks.load).not.toHaveBeenCalled();
});

test('retains native/WASM fallback after every embedded candidate fails decoding', async () => {
    mocks.embedded.mockImplementation(async function* () { yield {blob: new Blob(['bad'])}; });
    mocks.bitmap.mockRejectedValueOnce(new Error('Invalid JPEG'));
    const video = new EventTarget() as HTMLVideoElement;
    Object.assign(video, {pause: vi.fn(), load: vi.fn(), removeAttribute: vi.fn()});
    Object.defineProperty(video, 'src', {set: () => queueMicrotask(() => video.dispatchEvent(new Event('error')))});
    const create = vi.spyOn(document, 'createElement').mockReturnValue(video);
    try {
        expect(await prepareVideoThumbnail(new File(['video'], 'clip.crm'), new AbortController().signal)).toMatchObject({runtime: 'ffmpeg_wasm'});
        expect(mocks.load).toHaveBeenCalledOnce();
    } finally { create.mockRestore(); }
});
