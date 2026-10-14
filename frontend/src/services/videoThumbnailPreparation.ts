import {getFileExtension} from '../utils/photoDisplay';
import {VIDEO_MAX_SOURCE_BYTES} from '../utils/videoMetadata';
import {embeddedVideoJpegs} from '../utils/videoEmbeddedThumbnail';
import {generateUploadThumbnail, uploadDerivativeSize, UPLOAD_THUMBNAIL_MAX_EDGE, type UploadDerivative} from '../utils/uploadDerivatives';

export const VIDEO_MAX_FRAME_PIXELS = 40_000_000;
const aborted = () => new DOMException('Video preparation aborted', 'AbortError');

export async function nativeVideoThumbnail(file: File, signal: AbortSignal): Promise<UploadDerivative> {
    if (signal.aborted) throw aborted();
    const video = document.createElement('video');
    const url = URL.createObjectURL(file);
    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    const wait = (event: 'loadeddata' | 'seeked') => new Promise<void>((resolve, reject) => {
        const done = () => { cleanup(); resolve(); };
        const fail = () => { cleanup(); reject(new Error('Video decode unsupported')); };
        const abort = () => { cleanup(); reject(aborted()); };
        const cleanup = () => {
            clearTimeout(timer);
            video.removeEventListener(event, done);
            video.removeEventListener('error', fail);
            signal.removeEventListener('abort', abort);
        };
        const timer = setTimeout(fail, 8000);
        video.addEventListener(event, done, {once: true});
        video.addEventListener('error', fail, {once: true});
        signal.addEventListener('abort', abort, {once: true});
        if (signal.aborted) abort();
    });
    try {
        const loaded = wait('loadeddata');
        video.preload = 'auto';
        video.src = url;
        await loaded;
        if (video.videoWidth * video.videoHeight > VIDEO_MAX_FRAME_PIXELS) throw new Error('Video resource guard');
        const position = Number.isFinite(video.duration) ? Math.min(1, video.duration * .1) : 0;
        if (position > 0) { const seeking = wait('seeked'); video.currentTime = position; await seeking; }
        const {width, height} = uploadDerivativeSize(video.videoWidth, video.videoHeight, UPLOAD_THUMBNAIL_MAX_EDGE);
        const canvas = new OffscreenCanvas(width, height);
        const context = canvas.getContext('2d') as OffscreenCanvasRenderingContext2D | null;
        if (!context) throw new Error('Video canvas unavailable');
        context.drawImage(video, 0, 0, width, height);
        return await generateUploadThumbnail(canvas);
    } finally {
        video.pause(); video.removeAttribute('src'); video.load(); URL.revokeObjectURL(url);
    }
}

export async function wasmVideoThumbnail(file: File, signal: AbortSignal): Promise<UploadDerivative> {
    if (signal.aborted) throw aborted();
    const {FFmpeg} = await import('@ffmpeg/ffmpeg');
    const ffmpeg = new FFmpeg();
    const abort = () => ffmpeg.terminate();
    signal.addEventListener('abort', abort, {once: true});
    try {
        if (signal.aborted) throw aborted();
        await ffmpeg.load({classWorkerURL: new URL('/video-runtime/ffmpeg-0.12.15/worker.js', location.origin).href,
            coreURL: new URL('/video-runtime/ffmpeg-core-0.12.10.js', location.origin).href,
            wasmURL: new URL('/video-runtime/ffmpeg-core-0.12.10.wasm', location.origin).href});
        await ffmpeg.createDir('/input');
        // WORKERFS reads the File on demand; never copy a multi-GB video into MEMFS.
        const source = new File([file], 'source.' + getFileExtension(file.name), {type: file.type});
        if (!await ffmpeg.mount('WORKERFS' as import('@ffmpeg/ffmpeg').FFFSType, {files: [source]}, '/input')) throw new Error('Video mount unsupported');
        const probeCode = await ffmpeg.ffprobe(['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
            '-of', 'json', '/input/' + source.name, '-o', '/dimensions.json'], 8000);
        // Pinned core 0.12.10 returns -1 for successful ffprobe (upstream #817).
        // Require fresh, valid bounded dimensions; timeout/error codes still fail.
        if (probeCode !== 0 && probeCode !== -1) throw new Error('Video decode unsupported');
        const probe = JSON.parse(String(await ffmpeg.readFile('/dimensions.json', 'utf8')));
        const {width, height} = probe.streams?.[0] || {};
        if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0) {
            throw new Error('Video decode unsupported');
        }
        if (width * height > VIDEO_MAX_FRAME_PIXELS) throw new Error('Video resource guard');
        for (const position of ['1', '0']) {
            const code = await ffmpeg.exec(['-v', 'error', '-threads', '1', '-ss', position, '-i', '/input/' + source.name,
                '-map', '0:v:0', '-frames:v', '1', '-an', '-sn', '-dn', '-vf',
                // Display aspect ratio includes non-square pixels and decoder autorotation.
                "scale='if(gte(dar,1),120,max(1,round(120*dar)))':'if(gte(dar,1),max(1,round(120/dar)),120)',setsar=1",
                '-q:v', '4', '/thumbnail.jpg'], 10000);
            if (code !== 0) continue;
            let bytes;
            try { bytes = await ffmpeg.readFile('/thumbnail.jpg'); }
            catch { continue; } // Successful seek may reach EOF without producing a frame.
            if (!(bytes instanceof Uint8Array) || !bytes.length) continue;
            const pixels = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
            try { return await generateUploadThumbnail(pixels); } finally { pixels.close(); }
        }
        throw new Error('Video decode unsupported');
    } finally { signal.removeEventListener('abort', abort); ffmpeg.terminate(); }
}

export async function prepareVideoThumbnail(file: File, signal: AbortSignal): Promise<{
    thumbnail: UploadDerivative; runtime: 'embedded_video_jpeg' | 'native_video' | 'ffmpeg_wasm'; readBytes?: number; readCalls?: number;
}> {
    if (signal.aborted) throw aborted();
    if (file.size <= 0 || file.size > VIDEO_MAX_SOURCE_BYTES) throw new Error('Video resource guard');
    for await (const image of embeddedVideoJpegs(file, signal, VIDEO_MAX_FRAME_PIXELS)) {
        let pixels: ImageBitmap | undefined;
        try {
            pixels = await createImageBitmap(image.blob);
            if (signal.aborted) throw aborted();
            if (pixels.width <= 0 || pixels.height <= 0 || pixels.width * pixels.height > VIDEO_MAX_FRAME_PIXELS) continue;
            const thumbnail = await generateUploadThumbnail(pixels);
            if (signal.aborted) throw aborted();
            return {thumbnail, runtime: 'embedded_video_jpeg', readBytes: image.readBytes, readCalls: image.readCalls};
        } catch {
            if (signal.aborted) throw aborted();
        } finally { pixels?.close(); }
    }
    try { return {thumbnail: await nativeVideoThumbnail(file, signal), runtime: 'native_video'}; }
    catch (error) {
        if (signal.aborted) throw aborted();
        if (error instanceof Error && error.message.includes('resource guard')) throw error;
        return {thumbnail: await wasmVideoThumbnail(file, signal), runtime: 'ffmpeg_wasm'};
    }
}
