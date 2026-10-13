import {Blob as NodeBlob} from 'node:buffer';
import {
    eligibleUploadJpeg, generateUploadPreview, generateUploadThumbnail, uploadDerivativeSize,
    validUploadDerivative, UPLOAD_PREVIEW_MAX_BYTES,
} from './uploadDerivatives';

let encodes: Array<{width: number; height: number; quality?: number}>;
let encoder: (width: number, height: number, quality?: number) => Blob;

beforeEach(() => {
    encodes = [];
    vi.stubGlobal('Blob', NodeBlob);
    encoder = () => new Blob(['jpeg'], {type: 'image/jpeg'});
    vi.stubGlobal('OffscreenCanvas', class {
        constructor(public width: number, public height: number) {}
        getContext() { return {drawImage: vi.fn()}; }
        async convertToBlob({quality}: {quality?: number}) {
            encodes.push({width: this.width, height: this.height, quality});
            return encoder(this.width, this.height, quality);
        }
    });
});
afterEach(() => vi.unstubAllGlobals());

test('bounds dimensions without upscaling', () => {
    expect(uploadDerivativeSize(6000, 4000, 2048)).toEqual({width: 2048, height: 1365});
    expect(uploadDerivativeSize(4000, 6000, 2048)).toEqual({width: 1365, height: 2048});
    expect(uploadDerivativeSize(40, 20, 2048)).toEqual({width: 40, height: 20});
    expect(() => uploadDerivativeSize(Infinity, 20, 2048)).toThrow();
    expect(() => uploadDerivativeSize(0, 20, 2048)).toThrow();
});

test('reduces JPEG quality to meet the strict decimal byte cap', async () => {
    encoder = (_, __, quality) => new Blob([new Uint8Array(quality === .9 ? 1_500_001 : 1_500_000)], {type: 'image/jpeg'});
    const result = await generateUploadPreview({width: 6000, height: 4000} as OffscreenCanvas);
    expect(result.blob.size).toBe(UPLOAD_PREVIEW_MAX_BYTES);
    expect(result.quality).toBe(.82);
    expect(encodes).toHaveLength(2);
});

test('reduces pixels when the quality ladder cannot meet the cap', async () => {
    encoder = width => new Blob([new Uint8Array(width === 2048 ? 1_500_001 : 20)], {type: 'image/jpeg'});
    const result = await generateUploadPreview({width: 6000, height: 4000} as OffscreenCanvas);
    expect(result.width).toBe(1638);
    expect(encodes).toHaveLength(6);
});

test('fails rather than returning an oversized preview', async () => {
    encoder = () => new Blob([new Uint8Array(1_500_001)], {type: 'image/jpeg'});
    await expect(generateUploadPreview({width: 6000, height: 4000} as OffscreenCanvas)).rejects.toThrow('strict byte cap');
    expect(encodes).toHaveLength(60);
});

test('generates a bounded thumbnail', async () => {
    const result = await generateUploadThumbnail({width: 6000, height: 4000} as OffscreenCanvas);
    expect([result.width, result.height, result.quality]).toEqual([120, 80, .65]);
});

test('JPEG pass-through uses bytes, not filename or content type', async () => {
    expect(await eligibleUploadJpeg(new Blob([new Uint8Array([255, 216, 1])], {type: 'image/png'}), 200, 100)).toBe(true);
    expect(await eligibleUploadJpeg(new Blob(['png'], {type: 'image/jpeg'}), 200, 100)).toBe(false);
    expect(await eligibleUploadJpeg(new Blob([new Uint8Array([255, 216])]), 2049, 100)).toBe(false);
    expect(await eligibleUploadJpeg(new Blob([new Uint8Array(1_500_001)]), 200, 100)).toBe(false);
});

test('validates worker results before accepting a derivative', () => {
    const value = {blob: new Blob(['jpeg'], {type: 'image/jpeg'}), width: 2048, height: 100, quality: .9};
    expect(validUploadDerivative(value, 2048, 1_500_000)).toBe(true);
    expect(validUploadDerivative({...value, width: 2049}, 2048, 1_500_000)).toBe(false);
    expect(validUploadDerivative({...value, height: 1.5}, 2048, 1_500_000)).toBe(false);
    expect(validUploadDerivative({...value, blob: new Blob([])}, 2048, 1_500_000)).toBe(false);
});
