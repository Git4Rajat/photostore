import mediaInfoFactory from 'mediainfo.js';
import {isVideoFilename} from './photoDisplay';
import type {UploadMetadataPacket} from './uploadMetadata';

export const VIDEO_MAX_SOURCE_BYTES = 10 * 1024 * 1024 * 1024;
export const VIDEO_METADATA_MAX_BYTES = 1024 * 1024;
export const VIDEO_METADATA_READ_BYTES = 32 * 1024 * 1024;
export const VIDEO_METADATA_MAX_READS = 2048;

export async function createVideoMetadata(file: File, sourceSha256: string): Promise<UploadMetadataPacket> {
    if (!isVideoFilename(file.name) || !/^[a-f0-9]{64}$/.test(sourceSha256)) throw new Error('Video metadata unsupported');
    if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > VIDEO_MAX_SOURCE_BYTES) throw new Error('Video resource guard');
    const parser = await mediaInfoFactory({format: 'object', full: true, coverData: false,
        chunkSize: 256 * 1024, locateFile: () => new URL('/video-runtime/mediainfo-0.3.8.wasm', self.location.origin).href});
    let readBytes = 0, readCalls = 0, readBudgetReached = false;
    try {
        const result = await parser.analyzeData(file.size, async (length, offset) => {
            if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length <= 0
                || offset + length > file.size || length > 256 * 1024) throw new Error('Video metadata reference');
            // An empty chunk finalizes MediaInfo's bounded analysis, retaining header fields.
            if (readCalls >= VIDEO_METADATA_MAX_READS || readBytes + length > VIDEO_METADATA_READ_BYTES) {
                readBudgetReached = true;
                return new Uint8Array();
            }
            readCalls++; readBytes += length;
            return new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
        });
        const tracks = result.media?.track;
        if (!Array.isArray(tracks) || tracks.length > 64 || !tracks.some(track => track['@type'] === 'Video')
            || !tracks.some(track => track['@type'] === 'General' && track.Format)) {
            throw new Error(readBudgetReached ? 'Video metadata budget' : 'Video metadata unsupported');
        }
        // Transport parser output, not a fabricated claim that all camera EXIF exists.
        const bytes = new TextEncoder().encode(JSON.stringify({version: 1, kind: 'video-mediainfo-json',
            parser: 'mediainfo.js@0.3.8', sourceSize: file.size, sourceSha256, readBudgetReached, tracks}));
        if (bytes.length > VIDEO_METADATA_MAX_BYTES) throw new Error('Video metadata budget');
        const blob = new Blob([bytes], {type: 'application/octet-stream'});
        const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
        const sha256 = Array.from(hash, byte => byte.toString(16).padStart(2, '0')).join('');
        return {blob, descriptor: {version: 1, bytes: blob.size, sha256, sourceSha256}, readBytes, readCalls, readBudgetReached};
    } finally { parser.close(); }
}
