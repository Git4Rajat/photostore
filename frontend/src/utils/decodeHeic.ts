import libheifModule from 'libheif-js/wasm-bundle.js';

type DisplayResult = {data: Uint8ClampedArray; width: number; height: number};
type HeifImage = {
    get_width(): number;
    get_height(): number;
    display(target: DisplayResult, callback: (result: DisplayResult | null) => void): void;
    free?: () => void;
};
const libheif = libheifModule as unknown as {HeifDecoder: new () => {decode(buffer: Uint8Array): HeifImage[]}};

export async function decodeHeic(buffer: ArrayBuffer, maxMegapixels = Infinity): Promise<{width: number; height: number; buffer: ArrayBuffer}> {
    const decoder = new libheif.HeifDecoder();
    const images = decoder.decode(new Uint8Array(buffer));
    const image = images?.[0];
    for (const unused of images?.slice(1) || []) unused.free?.();
    if (!image) throw new Error('heic_no_image');
    try {
        const width = image.get_width(), height = image.get_height();
        if (![width, height].every(value => Number.isSafeInteger(value) && value > 0)) throw new Error('heic_invalid_dimensions');
        if (!Number.isSafeInteger(width * height) || width * height > maxMegapixels * 1_000_000) {
            throw new Error('Resource guard: HEIF pixel limit');
        }
        // libheif applies irot/imir; these pixels must not be rotated a second time.
        const rgba = new Uint8ClampedArray(width * height * 4);
        const result = await new Promise<DisplayResult>((resolve, reject) => {
            image.display({data: rgba, width, height}, result => result ? resolve(result) : reject(new Error('heic_decode_failed')));
        });
        return {width, height, buffer: result.data.buffer as ArrayBuffer};
    } finally {
        image.free?.();
    }
}
