import {generateUploadThumbnail, type UploadDerivative} from '../utils/uploadDerivatives';

export type VideoThumbnail = UploadDerivative & {readBytes?: number; readCalls?: number};

export async function proresRawThumbnail(file: File, signal: AbortSignal): Promise<VideoThumbnail> {
    if (signal.aborted) throw new DOMException('Video preparation aborted', 'AbortError');
    const worker = new Worker(new URL('/video-runtime/prores-raw-9.0-r1/thumbnail.worker.mjs', location.origin), {type: 'module'});
    let abort: () => void = () => {};
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
        const result = await new Promise<{bytes: Uint8Array; readBytes: number; readCalls: number}>((resolve, reject) => {
            abort = () => reject(new DOMException('Video preparation aborted', 'AbortError'));
            signal.addEventListener('abort', abort, {once: true});
            timer = setTimeout(() => reject(new Error('Video generation timeout')), 30000);
            worker.onerror = () => reject(new Error('ProRes RAW worker error'));
            worker.onmessage = ({data}) => {
                if (data?.status !== 'generated') { reject(new Error(data?.error || 'ProRes RAW decode failed')); return; }
                if (!(data.bytes instanceof Uint8Array) || !data.bytes.length || data.bytes.length > 512000
                    || !Number.isSafeInteger(data.readBytes) || data.readBytes < 0 || data.readBytes > 64 * 1024 * 1024
                    || !Number.isSafeInteger(data.readCalls) || data.readCalls < 0 || data.readCalls > 2048) {
                    reject(new Error('Invalid video derivative')); return;
                }
                resolve(data);
            };
            if (signal.aborted) abort();
            else worker.postMessage({file});
        });
        if (signal.aborted) throw new DOMException('Video preparation aborted', 'AbortError');
        const pixels = await createImageBitmap(new Blob([result.bytes], {type: 'image/jpeg'}));
        try {
            if (pixels.width < 1 || pixels.height < 1 || Math.max(pixels.width, pixels.height) > 120) throw new Error('Invalid video derivative');
            const thumbnail = await generateUploadThumbnail(pixels);
            if (signal.aborted) throw new DOMException('Video preparation aborted', 'AbortError');
            return {...thumbnail, readBytes: result.readBytes, readCalls: result.readCalls};
        } finally { pixels.close(); }
    } finally {
        clearTimeout(timer);
        signal.removeEventListener('abort', abort);
        worker.terminate();
    }
}
