export const UPLOAD_PREVIEW_MAX_EDGE = 2048;
export const UPLOAD_PREVIEW_MAX_BYTES = 1_500_000;
export const UPLOAD_THUMBNAIL_MAX_EDGE = 120;
export const UPLOAD_THUMBNAIL_MAX_BYTES = 512_000;

type Source = (ImageBitmap | OffscreenCanvas);
export interface UploadDerivative {
    blob: Blob;
    width: number;
    height: number;
    quality: number | null;
    passthrough?: boolean;
}

export const uploadDerivativeSize = (width: number, height: number, edge: number) => {
    if (![width, height, edge].every(value => Number.isFinite(value) && value > 0)) {
        throw new Error('Invalid image dimensions');
    }
    const scale = Math.min(1, edge / Math.max(width, height));
    return {width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale))};
};

export async function encodeUploadDerivative(source: Source, edge: number, quality: number): Promise<UploadDerivative> {
    const {width, height} = uploadDerivativeSize(source.width, source.height, edge);
    const canvas = new OffscreenCanvas(width, height) as OffscreenCanvas & {
        convertToBlob(options: {type: string; quality: number}): Promise<Blob>;
    };
    const context = canvas.getContext('2d') as OffscreenCanvasRenderingContext2D | null;
    if (!context) throw new Error('Canvas context unavailable');
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(source, 0, 0, width, height);
    const blob = await canvas.convertToBlob({type: 'image/jpeg', quality});
    if (blob.type !== 'image/jpeg' || blob.size === 0) throw new Error('JPEG encoder unavailable');
    return {blob, width, height, quality};
}

export async function generateUploadPreview(source: Source): Promise<UploadDerivative> {
    let edge = UPLOAD_PREVIEW_MAX_EDGE;
    for (let shrink = 0; shrink < 12; shrink++) {
        for (const quality of [0.9, 0.82, 0.74, 0.66, 0.58]) {
            const result = await encodeUploadDerivative(source, edge, quality);
            if (result.blob.size <= UPLOAD_PREVIEW_MAX_BYTES) return result;
        }
        edge = Math.max(1, Math.floor(edge * 0.8));
    }
    throw new Error('Preview cannot meet strict byte cap');
}

export async function generateUploadThumbnail(source: Source): Promise<UploadDerivative> {
    const result = await encodeUploadDerivative(source, UPLOAD_THUMBNAIL_MAX_EDGE, 0.65);
    if (result.blob.size > UPLOAD_THUMBNAIL_MAX_BYTES) throw new Error('Thumbnail exceeds byte cap');
    return result;
}

export async function eligibleUploadJpeg(source: Blob, width: number, height: number): Promise<boolean> {
    if (![width, height].every(value => Number.isInteger(value) && value > 0)
        || source.size === 0 || source.size > UPLOAD_PREVIEW_MAX_BYTES || Math.max(width, height) > UPLOAD_PREVIEW_MAX_EDGE) return false;
    const signature = new Uint8Array(await source.slice(0, 2).arrayBuffer());
    return signature[0] === 255 && signature[1] === 216;
}

export function validUploadDerivative(value: unknown, edge: number, bytes: number): value is UploadDerivative {
    const item = value as UploadDerivative | undefined;
    return !!item && item.blob instanceof Blob && item.blob.type === 'image/jpeg'
        && item.blob.size > 0 && item.blob.size <= bytes
        && Number.isInteger(item.width) && Number.isInteger(item.height)
        && item.width > 0 && item.height > 0 && Math.max(item.width, item.height) <= edge;
}
