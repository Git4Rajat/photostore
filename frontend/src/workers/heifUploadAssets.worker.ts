import {decodeHeic} from '../utils/decodeHeic';
import {generateUploadPreview, generateUploadThumbnail} from '../utils/uploadDerivatives';
import {preparationFailureReason} from '../utils/uploadPreparationDiagnostics';

self.onmessage = async ({data: {file, maxMegapixels = 120, maxBytes = 256 * 1024 * 1024}}: MessageEvent) => {
    try {
        if (file.size > maxBytes) throw new Error('Resource guard: HEIF byte limit');
        const decoded = await decodeHeic(await file.arrayBuffer(), maxMegapixels);
        const canvas = new OffscreenCanvas(decoded.width, decoded.height);
        const context = canvas.getContext('2d') as OffscreenCanvasRenderingContext2D | null;
        if (!context) throw new Error('Canvas context unavailable');
        context.putImageData(new ImageData(new Uint8ClampedArray(decoded.buffer), decoded.width, decoded.height), 0, 0);
        const preview = await generateUploadPreview(canvas);
        const thumbnail = await generateUploadThumbnail(canvas);
        self.postMessage({status: 'generated', preview, thumbnail, source: 'browser_heif'});
    } catch (error) {
        self.postMessage({status: 'fallback', reason: 'generation_failed', failureReason: preparationFailureReason(error)});
    }
};
