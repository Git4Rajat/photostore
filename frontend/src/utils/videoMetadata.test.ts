import {Blob as NodeBlob, File as NodeFile} from 'node:buffer';
import {webcrypto} from 'node:crypto';
const mocks = vi.hoisted(() => ({factory: vi.fn(), analyze: vi.fn(), close: vi.fn()}));
vi.mock('mediainfo.js', () => ({default: mocks.factory}));
import {createVideoMetadata, VIDEO_MAX_SOURCE_BYTES} from './videoMetadata';
import {SUPPORTED_VIDEO_EXTENSIONS} from './photoDisplay';

const tracks = [{ '@type': 'General', Format: 'MPEG-4', Encoded_Date: '2024-01-02 03:04:05 UTC' },
    {'@type': 'Video', Width: 1920, Height: 1080, Duration: 2, Rotation: 90, Format: 'AVC'}];
beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('Blob', NodeBlob); vi.stubGlobal('File', NodeFile); vi.stubGlobal('crypto', webcrypto);
    mocks.factory.mockResolvedValue({analyzeData: mocks.analyze, close: mocks.close});
    mocks.analyze.mockImplementation(async (_size, read) => { await read(4, 0); return {media: {track: tracks}}; });
});
afterEach(() => vi.unstubAllGlobals());

test.each(SUPPORTED_VIDEO_EXTENSIONS)('transports parsed %s metadata with bound source identity', async extension => {
    const packet = await createVideoMetadata(new File(['video-data'], 'clip.' + extension), 'a'.repeat(64));
    const body = JSON.parse(await packet.blob.text());
    expect(body).toMatchObject({kind: 'video-mediainfo-json', version: 1, sourceSize: 10, sourceSha256: 'a'.repeat(64), tracks});
    expect(packet.descriptor.bytes).toBe(packet.blob.size);
    expect(packet.descriptor.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(packet).toMatchObject({readBytes: 4, readCalls: 1});
    expect(mocks.close).toHaveBeenCalledOnce();
    expect(mocks.factory).toHaveBeenCalledWith(expect.objectContaining({full: true, coverData: false, chunkSize: 262144}));
});

test.each([{}, {media: {track: []}}, {media: {track: [{'@type': 'Audio'}]}}])('does not claim success for unrecognized media', async result => {
    mocks.analyze.mockResolvedValue(result);
    await expect(createVideoMetadata(new File(['bad'], 'clip.mp4'), 'a'.repeat(64))).rejects.toThrow('unsupported');
    expect(mocks.close).toHaveBeenCalledOnce();
});

test.each([[4, -1], [4, 1], [0, 0], [Infinity, 0]])('rejects invalid parser read request', async (length, offset) => {
    mocks.analyze.mockImplementation((_size, read) => read(length, offset));
    await expect(createVideoMetadata(new File(['data'], 'clip.mov'), 'a'.repeat(64))).rejects.toThrow('reference');
    expect(mocks.close).toHaveBeenCalledOnce();
});

test('rejects too-large sources before instantiating the parser', async () => {
    const file = new File(['data'], 'clip.mp4');
    Object.defineProperty(file, 'size', {value: VIDEO_MAX_SOURCE_BYTES + 1});
    await expect(createVideoMetadata(file, 'a'.repeat(64))).rejects.toThrow('resource guard');
    expect(mocks.factory).not.toHaveBeenCalled();
});

test('finalizes bounded parser reads while preserving usable partial metadata', async () => {
    mocks.analyze.mockImplementation(async (_size, read) => {
        for (let i = 0; i < 2050; i++) if (!(await read(1, 0)).length) break;
        return {media: {track: tracks}};
    });
    const packet = await createVideoMetadata(new File(['data'], 'clip.mkv'), 'a'.repeat(64));
    expect(packet).toMatchObject({readCalls: 2048, readBytes: 2048, readBudgetReached: true});
    expect(JSON.parse(await packet.blob.text()).readBudgetReached).toBe(true);
    expect(mocks.close).toHaveBeenCalledOnce();
});

test('does not claim usable metadata if the read budget ends before video recognition', async () => {
    mocks.analyze.mockImplementation(async (_size, read) => {
        for (let i = 0; i < 2050; i++) if (!(await read(1, 0)).length) break;
        return {media: {track: [{'@type': 'General'}]}};
    });
    await expect(createVideoMetadata(new File(['data'], 'clip.mkv'), 'a'.repeat(64))).rejects.toThrow('budget');
    expect(mocks.close).toHaveBeenCalledOnce();
});

test('stops at 32 MiB of actual reads without discarding parsed headers', async () => {
    const file = new File([new Uint8Array(34 * 1024 * 1024)], 'clip.mov');
    const slice = vi.spyOn(file, 'slice');
    mocks.analyze.mockImplementation(async (_size, read) => {
        while ((await read(256 * 1024, 0)).length) { /* MediaInfo may reread ranges. */ }
        return {media: {track: tracks}};
    });
    const packet = await createVideoMetadata(file, 'a'.repeat(64));
    expect(packet).toMatchObject({readBytes: 32 * 1024 * 1024, readCalls: 128, readBudgetReached: true});
    expect(slice).toHaveBeenCalledTimes(128);
});
