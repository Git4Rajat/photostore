import { readBlobArrayBuffer } from './blobIo';
import { isRawFilename } from './photoDisplay';

/**
 * Hand-rolled EXIF/GPS extraction covering JPEG (APP1 Exif segment) and RAW
 * formats (bare TIFF header, "Exif\0\0"-signed TIFF blocks, and ISO-BMFF/HEIC
 * box containers) -- no third-party EXIF library is used client-side, so
 * this parses just enough of each container to recover GPS + a handful of
 * display fields. Extracted from PhotoGallery.tsx's runBrowserProcessing.
 */

export interface ParsedGpsExif {
    exif: Record<string, string>;
    latitude?: string;
    longitude?: string;
    hasExif: boolean;
}

const readAscii = (view: DataView, offset: number, length: number) => {
    let value = '';
    for (let i = 0; i < length; i += 1) {
        value += String.fromCharCode(view.getUint8(offset + i));
    }
    return value;
};

const gpsRational = (view: DataView, offset: number, little: boolean) => {
    const numerator = view.getUint32(offset, little);
    const denominator = view.getUint32(offset + 4, little);
    return denominator === 0 ? 0 : numerator / denominator;
};

const TIFF_TYPE_BYTES: Record<number, number> = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    7: 1,
    9: 4,
    10: 8,
};

const TIFF_IFD0_TAGS: Record<number, string> = {
    0x010f: 'Make',
    0x0110: 'Model',
    0x0112: 'Orientation',
    0x0132: 'DateTime',
    0x8769: 'ExifIFDPointer',
};

const TIFF_EXIF_TAGS: Record<number, string> = {
    0x829a: 'ExposureTime',
    0x829d: 'FNumber',
    0x8827: 'ISOSpeedRatings',
    0x9003: 'DateTimeOriginal',
    0x920a: 'FocalLength',
    0xa002: 'ExifImageWidth',
    0xa003: 'ExifImageHeight',
    0xa405: 'FocalLengthIn35mmFilm',
    0xa434: 'LensModel',
};

const GPS_TAGS: Record<number, string> = {
    1: 'GPSLatitudeRef',
    2: 'GPSLatitude',
    3: 'GPSLongitudeRef',
    4: 'GPSLongitude',
    5: 'GPSAltitudeRef',
    6: 'GPSAltitude',
    7: 'GPSTimeStamp',
    29: 'GPSDateStamp',
};

const tiffValueOffset = (view: DataView, tiff: number, entry: number, type: number, count: number, little: boolean) => {
    const typeBytes = TIFF_TYPE_BYTES[type] || 0;
    if (!typeBytes || count < 0) {
        return -1;
    }
    const byteCount = typeBytes * count;
    if (byteCount <= 4) {
        return entry + 8;
    }
    return tiff + view.getUint32(entry + 8, little);
};

const tiffAscii = (view: DataView, offset: number, count: number) => {
    if (offset < 0 || count <= 0 || offset + count > view.byteLength) {
        return '';
    }
    return readAscii(view, offset, count).replace(/\0+$/, '').trim();
};

const tiffValueString = (view: DataView, offset: number, type: number, count: number, little: boolean) => {
    if (offset < 0 || offset >= view.byteLength) {
        return '';
    }
    try {
        if (type === 2) {
            return tiffAscii(view, offset, count);
        }
        if (type === 3 && offset + 2 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => (
                offset + idx * 2 + 2 <= view.byteLength ? String(view.getUint16(offset + idx * 2, little)) : ''
            )).filter(Boolean);
            return values.join(', ');
        }
        if (type === 4 && offset + 4 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => (
                offset + idx * 4 + 4 <= view.byteLength ? String(view.getUint32(offset + idx * 4, little)) : ''
            )).filter(Boolean);
            return values.join(', ');
        }
        if (type === 5 && offset + 8 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => {
                const valueOffset = offset + idx * 8;
                if (valueOffset + 8 > view.byteLength) {
                    return '';
                }
                const numerator = view.getUint32(valueOffset, little);
                const denominator = view.getUint32(valueOffset + 4, little);
                return denominator ? `${numerator}/${denominator}` : String(numerator);
            }).filter(Boolean);
            return values.join(', ');
        }
    } catch {
        return '';
    }
    return '';
};

const parseTiffGpsExif = (view: DataView, tiff: number): ParsedGpsExif | null => {
    if (tiff + 8 > view.byteLength) {
        return null;
    }
    const endian = readAscii(view, tiff, 2);
    const little = endian === 'II';
    if (!little && endian !== 'MM') {
        return null;
    }
    if (view.getUint16(tiff + 2, little) !== 42) {
        return null;
    }
    const ifd0 = tiff + view.getUint32(tiff + 4, little);
    if (ifd0 + 2 > view.byteLength) {
        return null;
    }
    const exif: Record<string, string> = {};
    const entries = view.getUint16(ifd0, little);
    let gpsIfd = 0;
    let exifIfd = 0;
    for (let i = 0; i < entries; i += 1) {
        const entry = ifd0 + 2 + i * 12;
        if (entry + 12 > view.byteLength) {
            break;
        }
        const tag = view.getUint16(entry, little);
        const type = view.getUint16(entry + 2, little);
        const count = view.getUint32(entry + 4, little);
        const valueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
        const tagName = TIFF_IFD0_TAGS[tag];
        if (tagName && tagName !== 'ExifIFDPointer') {
            const value = tiffValueString(view, valueOffset, type, count, little);
            if (value) {
                exif[tagName] = value;
            }
        }
        if (tag === 0x8825) {
            gpsIfd = tiff + view.getUint32(entry + 8, little);
        } else if (tag === 0x8769) {
            exifIfd = tiff + view.getUint32(entry + 8, little);
        }
    }
    if (exifIfd && exifIfd + 2 <= view.byteLength) {
        const exifEntries = view.getUint16(exifIfd, little);
        for (let i = 0; i < exifEntries; i += 1) {
            const entry = exifIfd + 2 + i * 12;
            if (entry + 12 > view.byteLength) {
                break;
            }
            const tag = view.getUint16(entry, little);
            const tagName = TIFF_EXIF_TAGS[tag];
            if (!tagName) {
                continue;
            }
            const type = view.getUint16(entry + 2, little);
            const count = view.getUint32(entry + 4, little);
            const valueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
            const value = tiffValueString(view, valueOffset, type, count, little);
            if (value) {
                exif[tagName] = value;
            }
        }
    }
    if (!gpsIfd || gpsIfd + 2 > view.byteLength) {
        return { exif, hasExif: true };
    }
    const gpsEntries = view.getUint16(gpsIfd, little);
    let latRef = 'N';
    let lonRef = 'E';
    let latValues: number[] | null = null;
    let lonValues: number[] | null = null;
    for (let i = 0; i < gpsEntries; i += 1) {
        const entry = gpsIfd + 2 + i * 12;
        if (entry + 12 > view.byteLength) {
            break;
        }
        const tag = view.getUint16(entry, little);
        const type = view.getUint16(entry + 2, little);
        const count = view.getUint32(entry + 4, little);
        const valueOffset = tiff + view.getUint32(entry + 8, little);
        const gpsName = GPS_TAGS[tag];
        if (gpsName) {
            const gpsValueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
            const value = tiffValueString(view, gpsValueOffset, type, count, little);
            if (value) {
                exif[`GPS.${gpsName}`] = value;
            }
        }
        if ((tag === 1 || tag === 3) && type === 2) {
            const ref = String.fromCharCode(view.getUint8(entry + 8));
            if (tag === 1) latRef = ref;
            if (tag === 3) lonRef = ref;
        }
        if ((tag === 2 || tag === 4) && type === 5 && count >= 3 && valueOffset + 24 <= view.byteLength) {
            const values = [0, 1, 2].map((idx) => gpsRational(view, valueOffset + idx * 8, little));
            if (tag === 2) latValues = values;
            if (tag === 4) lonValues = values;
        }
    }
    const toDecimal = (values: number[], ref: string) => {
        const decimal = values[0] + values[1] / 60 + values[2] / 3600;
        return (ref === 'S' || ref === 'W' ? -decimal : decimal).toFixed(7).replace(/\.?0+$/, '');
    };
    if (latValues && lonValues) {
        const latitude = toDecimal(latValues, latRef);
        const longitude = toDecimal(lonValues, lonRef);
        exif['GPS.GPSLatitudeRef'] = latRef;
        exif['GPS.GPSLongitudeRef'] = lonRef;
        exif['GPS.LatitudeDecimal'] = latitude;
        exif['GPS.LongitudeDecimal'] = longitude;
        return { exif, latitude, longitude, hasExif: true };
    }
    return { exif, hasExif: true };
};

export const parseJpegGpsExif = async (source: Blob | File, sourceName = ''): Promise<ParsedGpsExif | null> => {
    const contentType = source.type || '';
    if (!/^image\/jpe?g$/i.test(contentType) && !/\.(jpe?g)$/i.test(sourceName)) {
        return null;
    }
    const buffer = await readBlobArrayBuffer(source.slice(0, Math.min(source.size, 512 * 1024)));
    const view = new DataView(buffer);
    if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) {
        return null;
    }
    let offset = 2;
    while (offset + 4 < view.byteLength) {
        if (view.getUint8(offset) !== 0xff) {
            break;
        }
        const marker = view.getUint8(offset + 1);
        const size = view.getUint16(offset + 2);
        if (marker === 0xe1 && size > 8 && readAscii(view, offset + 4, 6) === 'Exif\0\0') {
            return parseTiffGpsExif(view, offset + 10) || { exif: {}, hasExif: true };
        }
        offset += 2 + size;
    }
    return { exif: {}, hasExif: false };
};

const bytesMatchAscii = (bytes: Uint8Array, offset: number, text: string) => {
    if (offset < 0 || offset + text.length > bytes.length) {
        return false;
    }
    for (let i = 0; i < text.length; i += 1) {
        if (bytes[offset + i] !== text.charCodeAt(i)) {
            return false;
        }
    }
    return true;
};

const isTiffHeaderAt = (bytes: Uint8Array, offset: number) => (
    offset >= 0
    && offset + 4 <= bytes.length
    && (
        (bytes[offset] === 0x49 && bytes[offset + 1] === 0x49 && bytes[offset + 2] === 0x2a && bytes[offset + 3] === 0x00)
        || (bytes[offset] === 0x4d && bytes[offset + 1] === 0x4d && bytes[offset + 2] === 0x00 && bytes[offset + 3] === 0x2a)
    )
);

const mergeParsedExif = (existing: ParsedGpsExif | null, next: ParsedGpsExif | null): ParsedGpsExif | null => {
    if (!next) {
        return existing;
    }
    if (!existing) {
        return next;
    }
    return {
        exif: { ...existing.exif, ...next.exif },
        latitude: existing.latitude || next.latitude,
        longitude: existing.longitude || next.longitude,
        hasExif: existing.hasExif || next.hasExif,
    };
};

const parseTiffCandidates = (view: DataView, bytes: Uint8Array, start = 0, end = bytes.length): ParsedGpsExif | null => {
    let best: ParsedGpsExif | null = null;
    const safeStart = Math.max(0, start);
    const safeEnd = Math.min(bytes.length - 4, end);
    for (let offset = safeStart; offset <= safeEnd; offset += 1) {
        if (!isTiffHeaderAt(bytes, offset)) {
            continue;
        }
        const parsed = parseTiffGpsExif(view, offset);
        if (!parsed) {
            continue;
        }
        best = mergeParsedExif(best, parsed);
        if (parsed.latitude && parsed.longitude) {
            return best;
        }
    }
    return best;
};

const parseExifSignatureCandidates = (view: DataView, bytes: Uint8Array): ParsedGpsExif | null => {
    let best: ParsedGpsExif | null = null;
    for (let offset = 0; offset <= bytes.length - 10; offset += 1) {
        if (!bytesMatchAscii(bytes, offset, 'Exif\0\0')) {
            continue;
        }
        const tiffOffset = offset + 6;
        if (!isTiffHeaderAt(bytes, tiffOffset)) {
            continue;
        }
        const parsed = parseTiffGpsExif(view, tiffOffset);
        if (!parsed) {
            continue;
        }
        best = mergeParsedExif(best, parsed);
        if (parsed.latitude && parsed.longitude) {
            return best;
        }
    }
    return best;
};

const parseIsoBmffExifCandidates = (view: DataView, bytes: Uint8Array, start = 0, end = bytes.length, depth = 0): ParsedGpsExif | null => {
    if (depth > 4) {
        return null;
    }
    let best: ParsedGpsExif | null = null;
    let offset = Math.max(0, start);
    const safeEnd = Math.min(bytes.length, end);
    const containerBoxes = new Set(['moov', 'trak', 'mdia', 'minf', 'stbl', 'meta', 'iprp', 'ipco', 'iinf']);
    while (offset + 8 <= safeEnd) {
        let size = view.getUint32(offset);
        const type = readAscii(view, offset + 4, 4);
        let header = 8;
        if (size === 1 && offset + 16 <= safeEnd) {
            const high = view.getUint32(offset + 8);
            const low = view.getUint32(offset + 12);
            if (high > 0 || low <= 16) {
                break;
            }
            size = low;
            header = 16;
        } else if (size === 0) {
            size = safeEnd - offset;
        }
        if (size < header || offset + size > safeEnd) {
            break;
        }
        const payloadStart = offset + header + (type === 'uuid' ? 16 : 0) + (type === 'meta' ? 4 : 0);
        const payloadEnd = offset + size;
        if (payloadStart < payloadEnd) {
            if (type.toLowerCase().includes('exif') || type === 'uuid') {
                best = mergeParsedExif(best, parseExifSignatureCandidates(view, bytes));
                best = mergeParsedExif(best, parseTiffCandidates(view, bytes, payloadStart, payloadEnd));
                if (best?.latitude && best.longitude) {
                    return best;
                }
            }
            if (containerBoxes.has(type)) {
                best = mergeParsedExif(best, parseIsoBmffExifCandidates(view, bytes, payloadStart, payloadEnd, depth + 1));
                if (best?.latitude && best.longitude) {
                    return best;
                }
            }
        }
        offset += size;
    }
    return best;
};

/** Max prefix of a RAW file scanned for embedded EXIF -- passed in by the
 * caller (PhotoGallery.tsx's CLIENT_RAW_EXIF_SCAN_MAX_BYTES) so this module
 * doesn't need its own copy of the shared MB-based sizing constant. */
export const parseRawGpsExif = async (file: File, maxScanBytes: number): Promise<ParsedGpsExif | null> => {
    if (!isRawFilename(file.name)) {
        return null;
    }
    const buffer = await readBlobArrayBuffer(file.slice(0, Math.min(file.size, maxScanBytes)));
    const view = new DataView(buffer);
    const bytes = new Uint8Array(buffer);
    const directTiff = parseTiffGpsExif(view, 0);
    if (directTiff?.latitude && directTiff.longitude) {
        return directTiff;
    }
    const exifMarker = parseExifSignatureCandidates(view, bytes);
    if (exifMarker?.latitude && exifMarker.longitude) {
        return exifMarker;
    }
    const isoExif = parseIsoBmffExifCandidates(view, bytes);
    if (isoExif?.latitude && isoExif.longitude) {
        return isoExif;
    }
    return mergeParsedExif(mergeParsedExif(directTiff, exifMarker), isoExif) || parseTiffCandidates(view, bytes);
};
