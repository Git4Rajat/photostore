import {vi} from 'vitest';
import {decodeHeic} from './decodeHeic';

const {decode} = vi.hoisted(() => ({decode: vi.fn()}));
vi.mock('libheif-js/wasm-bundle.js', () => ({default: {HeifDecoder: class {decode = decode;}}}));
const image = (width = 20, height = 10) => ({
    get_width: () => width, get_height: () => height, free: vi.fn(),
    display: vi.fn((target, callback) => callback(target)),
});

test('returns display-ready pixels and frees primary and auxiliary images', async () => {
    const primary = image(), auxiliary = image();
    decode.mockReturnValue([primary, auxiliary]);
    const result = await decodeHeic(new ArrayBuffer(4), 120);
    expect(result).toMatchObject({width: 20, height: 10});
    expect(result.buffer.byteLength).toBe(800);
    expect(primary.free).toHaveBeenCalledOnce();
    expect(auxiliary.free).toHaveBeenCalledOnce();
    expect(auxiliary.display).not.toHaveBeenCalled();
});

test.each([[0, 10], [1.5, 10], [NaN, 10], [Infinity, 10]])('rejects invalid dimensions %s/%s before display', async (width, height) => {
    const primary = image(width, height);
    decode.mockReturnValue([primary]);
    await expect(decodeHeic(new ArrayBuffer(4), 120)).rejects.toThrow('heic_invalid_dimensions');
    expect(primary.display).not.toHaveBeenCalled();
    expect(primary.free).toHaveBeenCalledOnce();
});

test('rejects oversized HEIF before allocating its pixel buffer', async () => {
    const primary = image(16000, 16000);
    decode.mockReturnValue([primary]);
    await expect(decodeHeic(new ArrayBuffer(4), 120)).rejects.toThrow('Resource guard');
    expect(primary.display).not.toHaveBeenCalled();
    expect(primary.free).toHaveBeenCalledOnce();
});

test('handles absent or failed decoded images', async () => {
    decode.mockReturnValue([]);
    await expect(decodeHeic(new ArrayBuffer(4))).rejects.toThrow('heic_no_image');
    const primary = image();
    primary.display.mockImplementation((_, callback) => callback(null));
    decode.mockReturnValue([primary]);
    await expect(decodeHeic(new ArrayBuffer(4))).rejects.toThrow('heic_decode_failed');
    expect(primary.free).toHaveBeenCalledOnce();
});
