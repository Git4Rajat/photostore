import {getFileExtension} from './photoDisplay';

export const EMBEDDED_VIDEO_READ_BYTES = 4 * 1024 * 1024;
export const EMBEDDED_VIDEO_IMAGE_BYTES = 2 * 1024 * 1024;
export const EMBEDDED_VIDEO_MAX_READS = 2048;
const MAX_BOXES = 1024;
const MAX_DEPTH = 8;
const CANON_CAMERA_UUID = '85c0b687820f11e08111f4ce462b6a48';
const CANON_PREVIEW_UUID = 'eaf42b5e1c984b88b9fbb7dc406e4d16';
const SOF = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);
const ascii = (bytes: Uint8Array, start: number, count: number) =>
    String.fromCharCode(...Array.from(bytes.subarray(start, start + count)));
const hex = (bytes: Uint8Array) => Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');

export interface EmbeddedVideoJpeg {
    blob: Blob;
    width: number;
    height: number;
    readBytes: number;
    readCalls: number;
}

function dimensions(bytes: Uint8Array, maxPixels: number): {width: number; height: number; length: number} | null {
    if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
    let end = bytes.length;
    while (end > bytes.length - 16 && bytes[end - 1] === 0) end--;
    // Canon THMB can include a bit-padding byte after the JPEG end marker.
    if (bytes[end - 1] === 0x80) end--;
    if (bytes[end - 2] !== 0xff || bytes[end - 1] !== 0xd9) return null;
    let pos = 2;
    for (let markers = 0; markers < 1024 && pos + 4 <= end; markers++) {
        if (bytes[pos++] !== 0xff) return null;
        while (bytes[pos] === 0xff) pos++;
        const marker = bytes[pos++];
        if (marker === 0xda || marker === 0xd9) return null;
        if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
        const length = bytes[pos] * 256 + bytes[pos + 1];
        if (length < 2 || pos + length > end) return null;
        if (SOF.has(marker)) {
            if (length < 8) return null;
            const height = bytes[pos + 3] * 256 + bytes[pos + 4];
            const width = bytes[pos + 5] * 256 + bytes[pos + 6];
            return width > 0 && height > 0 && width * height <= maxPixels ? {width, height, length: end} : null;
        }
        pos += length;
    }
    return null;
}

// Follow documented camera boxes, not JPEG signatures in compressed video data.
export async function* embeddedVideoJpegs(file: File, signal: AbortSignal, maxPixels: number): AsyncGenerator<EmbeddedVideoJpeg> {
    if (!['crm', 'nev', 'r3d'].includes(getFileExtension(file.name))) return;
    let readBytes = 0, readCalls = 0, boxes = 0;
    const checkAbort = () => { if (signal.aborted) throw new DOMException('Video preparation aborted', 'AbortError'); };
    const read = async (offset: number, length: number) => {
        checkAbort();
        if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0
            || offset + length > file.size || readBytes + length > EMBEDDED_VIDEO_READ_BYTES
            || readCalls >= EMBEDDED_VIDEO_MAX_READS) throw new Error('Embedded video read budget or reference');
        readBytes += length; readCalls++;
        const bytes = new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
        checkAbort();
        if (bytes.length !== length) throw new Error('Embedded video short read');
        return bytes;
    };
    const image = async (offset: number, length: number): Promise<EmbeddedVideoJpeg | null> => {
        if (length <= 0 || length > EMBEDDED_VIDEO_IMAGE_BYTES) return null;
        const bytes = await read(offset, length);
        const size = dimensions(bytes, maxPixels);
        return size ? {blob: new Blob([bytes.subarray(0, size.length)], {type: 'image/jpeg'}),
            width: size.width, height: size.height, readBytes, readCalls} : null;
    };
    type Context = 'root' | 'movie' | 'userdata' | 'canon' | 'nikon';
    async function* walk(start: number, end: number, context: Context, depth = 0): AsyncGenerator<EmbeddedVideoJpeg> {
        if (depth > MAX_DEPTH) throw new Error('Embedded video depth limit');
        for (let pos = start; pos + 8 <= end;) {
            if (++boxes > MAX_BOXES) throw new Error('Embedded video box limit');
            const header = await read(pos, 8);
            let length = new DataView(header.buffer).getUint32(0), headerSize = 8;
            const type = ascii(header, 4, 4);
            if (length === 1) {
                if (pos + 16 > end) throw new Error('Embedded video extended box');
                length = Number(new DataView((await read(pos + 8, 8)).buffer).getBigUint64(0));
                headerSize = 16;
            }
            if (length === 0) length = end - pos;
            if (!Number.isSafeInteger(length) || length < headerSize || pos + length > end) throw new Error('Embedded video invalid box');
            const payload = pos + headerSize, limit = pos + length, payloadSize = length - headerSize;
            if (type === 'moov' && context === 'root') yield* walk(payload, limit, 'movie', depth + 1);
            else if (type === 'udta' && context === 'movie') yield* walk(payload, limit, 'userdata', depth + 1);
            else if (type === 'NCDT' && context === 'userdata') yield* walk(payload, limit, 'nikon', depth + 1);
            else if (type === 'uuid' && (context === 'root' || context === 'movie') && payloadSize >= 16) {
                const uuid = hex(await read(payload, 16));
                if (uuid === CANON_CAMERA_UUID && context === 'movie') yield* walk(payload + 16, limit, 'canon', depth + 1);
                else if (uuid === CANON_PREVIEW_UUID && context === 'root' && payloadSize >= 48) {
                    const prefix = await read(payload + 16, 32), view = new DataView(prefix.buffer);
                    const size = view.getUint32(8), bytes = view.getUint32(28);
                    if (ascii(prefix, 12, 4) === 'PRVW' && size >= 24 && size <= payloadSize - 24 && bytes <= size - 24) {
                        const jpeg = await image(payload + 48, bytes);
                        if (jpeg) yield jpeg;
                    }
                }
            } else if (type === 'THMB' && context === 'canon' && payloadSize >= 16) {
                const prefix = await read(payload, 16), bytes = new DataView(prefix.buffer).getUint32(8);
                if (bytes <= payloadSize - 16) {
                    const jpeg = await image(payload + 16, bytes);
                    if (jpeg) yield jpeg;
                }
            } else if (context === 'nikon' && ['NCTH', 'NCVW'].includes(type)) {
                const jpeg = await image(payload, payloadSize);
                if (jpeg) yield jpeg;
            }
            // Other boxes, including mdat and track sample tables, are skipped by size.
            pos = limit;
        }
    }
    try {
        if (file.size < 16) return;
        const head = await read(0, 16);
        if (ascii(head, 4, 4) !== 'ftyp' || !['crx ', 'niko'].includes(ascii(head, 8, 4))) return;
        yield* walk(0, file.size, 'root');
    } catch (error) {
        checkAbort();
        // Missing/malformed/over-budget images leave native/WASM decoding available.
        if (error instanceof DOMException && error.name === 'AbortError') throw error;
    }
}
