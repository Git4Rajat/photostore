import {eligibleUploadJpeg, generateUploadPreview, generateUploadThumbnail} from '../utils/uploadDerivatives';
import {preparationFailureReason} from '../utils/uploadPreparationDiagnostics';

self.onmessage = async ({data: {file, preserveEligibleJpeg = true, maxMegapixels = 120, maxBytes = 256 * 1024 * 1024}}: MessageEvent) => {
    let bitmap: ImageBitmap | undefined;
    try {
        if (file.size > maxBytes) throw new Error('Resource guard: raster byte limit');
        // Native decoding applies EXIF orientation; the canvas bakes it once.
        bitmap = await createImageBitmap(file);
        if (bitmap.width * bitmap.height > maxMegapixels * 1_000_000) throw new Error('Resource guard: raster pixel limit');
        const encoded = await generateUploadPreview(bitmap);
        const preview = preserveEligibleJpeg && file.size < encoded.blob.size && await eligibleUploadJpeg(file, bitmap.width, bitmap.height)
            ? {blob: file.slice(0, file.size, 'image/jpeg'), width: bitmap.width, height: bitmap.height, quality: null, passthrough: true}
            : encoded;
        const thumbnail = await generateUploadThumbnail(bitmap);
        self.postMessage({status: 'generated', preview, thumbnail, source: 'browser_raster'});
    } catch (error) {
        self.postMessage({status: 'fallback', reason: 'generation_failed', failureReason: preparationFailureReason(error)});
    } finally {
        bitmap?.close();
    }
};
