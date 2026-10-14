import {createVideoMetadata} from '../src/utils/videoMetadata';
import {prepareVideoThumbnail, wasmVideoThumbnail} from '../src/services/videoThumbnailPreparation';
import {validUploadDerivative, UPLOAD_THUMBNAIL_MAX_BYTES, UPLOAD_THUMBNAIL_MAX_EDGE} from '../src/utils/uploadDerivatives';
import {UploadPreparationPipeline} from '../src/services/uploadPreparationPipeline';
import {embeddedVideoJpegs} from '../src/utils/videoEmbeddedThumbnail';

declare global {
    interface Window {
        runVideoSmoke: (hashes: Record<string, string>, pipelineMode?: boolean) => Promise<unknown[]>;
        saveVideoEvidence: (name: string, kind: string, bytes: number[]) => Promise<void>;
        videoProgress: (value: unknown) => Promise<void>;
    }
}


window.runVideoSmoke = async (hashes, pipelineMode = false) => {
    const files = [...document.querySelector('input')!.files!];
    const results = [];
    for (const file of files) {
        const row: Record<string, unknown> = {name: file.name};
        const start = performance.now();
        const pipeline = pipelineMode ? new UploadPreparationPipeline() : null;
        const receipt = '/receipts/' + encodeURIComponent(file.name) + '/';
        if (pipeline) {
            try {
                row.pipeline = await pipeline.start(file, hashes[file.name], {uploadPreparationEnabled: true,
                    metadataBlobUrl: receipt + 'metadata', thumbnailBlobUrl: receipt + 'thumbnail'}, new AbortController().signal);
            } finally { pipeline.dispose(); }
        }
        try {
            const prepared = row.pipeline as Awaited<ReturnType<UploadPreparationPipeline['start']>> | undefined;
            const packet = prepared ? prepared.clientMetadata ? {blob: await (await fetch(receipt + 'metadata')).blob(),
                descriptor: prepared.clientMetadata, readBytes: prepared.preparationReport?.assets.metadata.readBytes,
                readCalls: prepared.preparationReport?.assets.metadata.readCalls} : null : await createVideoMetadata(file, hashes[file.name]);
            if (!packet) throw new Error(prepared?.preparationReport?.assets.metadata.generationReason || 'Video metadata failed');
            row.metadata = {bytes: packet.blob.size, readBytes: packet.readBytes, readCalls: packet.readCalls,
                descriptor: packet.descriptor, parsed: JSON.parse(await packet.blob.text())};
            await window.saveVideoEvidence(file.name, 'json', [...new Uint8Array(await packet.blob.arrayBuffer())]);
        } catch (error) { row.metadataError = String(error); }
        const prepared = row.pipeline as Awaited<ReturnType<UploadPreparationPipeline['start']>> | undefined;
        if (prepared?.preparationReport?.assets.thumbnail.runtime === 'embedded_video_jpeg') {
            for await (const image of embeddedVideoJpegs(file, new AbortController().signal, 40_000_000)) {
                row.embeddedSource = {bytes: image.blob.size, width: image.width, height: image.height,
                    readBytes: image.readBytes, readCalls: image.readCalls};
                await window.saveVideoEvidence(file.name, 'embedded-source.jpg', [...new Uint8Array(await image.blob.arrayBuffer())]);
                break;
            }
        }
        for (const forced of [false, true]) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 45000);
            try {
                const prepared = row.pipeline as Awaited<ReturnType<UploadPreparationPipeline['start']>> | undefined;
                let result;
                if (!forced && prepared) {
                    if (!prepared.clientProcessing?.thumbnail) throw new Error(prepared.preparationReport?.assets.thumbnail.generationReason || 'Video thumbnail failed');
                    const blob = await (await fetch(receipt + 'thumbnail')).blob();
                    const bitmap = await createImageBitmap(blob);
                    result = {thumbnail: {blob, width: bitmap.width, height: bitmap.height},
                        runtime: prepared.preparationReport?.assets.thumbnail.runtime};
                    bitmap.close();
                } else result = forced ? {thumbnail: await wasmVideoThumbnail(file, controller.signal), runtime: 'ffmpeg_wasm'}
                    : await prepareVideoThumbnail(file, controller.signal);
                const thumb = result.thumbnail;
                if (!validUploadDerivative(thumb, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES)) throw new Error('Invalid thumbnail');
                const bitmap = await createImageBitmap(thumb.blob);
                const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
                const ctx = canvas.getContext('2d') as OffscreenCanvasRenderingContext2D;
                ctx.drawImage(bitmap, 0, 0); bitmap.close();
                const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                let minimum = 255, maximum = 0;
                for (let i = 0; i < pixels.length; i++) if (i % 4 !== 3) {
                    minimum = Math.min(minimum, pixels[i]); maximum = Math.max(maximum, pixels[i]);
                }
                const range = maximum - minimum;
                row[forced ? 'wasm' : 'automatic'] = {runtime: result.runtime, bytes: thumb.blob.size,
                    width: thumb.width, height: thumb.height, nonblank: range > 20};
                await window.saveVideoEvidence(file.name, forced ? 'wasm.jpg' : 'jpg', [...new Uint8Array(await thumb.blob.arrayBuffer())]);
            } catch (error) { row[forced ? 'wasmError' : 'automaticError'] = String(error); }
            finally { clearTimeout(timer); }
        }
        row.elapsedMs = Math.round(performance.now() - start);
        results.push(row);
        await window.videoProgress(row);
    }
    return results;
};
