import {eligibleUploadJpeg, generateUploadPreview, generateUploadThumbnail} from '../utils/uploadDerivatives';

self.onmessage = async ({data: {file, preserveEligibleJpeg = false, maxMegapixels = 120}}: MessageEvent) => {
    let bitmap: ImageBitmap | undefined;
    try {
        // Native decoding applies EXIF orientation; the canvas bakes it once.
        bitmap = await createImageBitmap(file);
        if (bitmap.width * bitmap.height > maxMegapixels * 1_000_000) throw new Error('Resource guard: raster pixel limit');
        const preview = preserveEligibleJpeg && await eligibleUploadJpeg(file, bitmap.width, bitmap.height)
            ? {blob: file.slice(0, file.size, 'image/jpeg'), width: bitmap.width, height: bitmap.height, quality: null, passthrough: true}
            : await generateUploadPreview(bitmap);
        const thumbnail = await generateUploadThumbnail(bitmap);
        self.postMessage({status: 'generated', preview, thumbnail, source: 'browser_raster'});
    } catch {
        self.postMessage({status: 'fallback', reason: 'generation_failed'});
    } finally {
        bitmap?.close();
    }
};
