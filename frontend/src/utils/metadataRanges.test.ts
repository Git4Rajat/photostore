import {Blob as NodeBlob} from 'node:buffer';
import {extractRanges} from './metadataRanges';

type Entry = [number, number, number, number];
const ifd = (view: DataView, offset: number, entries: Entry[], little = true) => {
    view.setUint16(offset, entries.length, little);
    entries.forEach(([tag, type, count, value], index) => {
        const pos = offset + 2 + index * 12;
        view.setUint16(pos, tag, little);
        view.setUint16(pos + 2, type, little);
        view.setUint32(pos + 4, count, little);
        view.setUint32(pos + 8, value, little);
    });
};
const rebuild = async (bytes: Uint8Array, maxBytes?: number) => {
    const result = await extractRanges(new NodeBlob([bytes]), maxBytes);
    const copy = new Uint8Array(bytes.length);
    for (const chunk of result.chunks) copy.set(chunk.bytes, chunk.offset);
    return {copy, result};
};

function plainJpeg(scanSize = 64, afterScan: number[] = []) {
    const bytes = [255, 216];
    const segment = (marker: number, data: number[]) => bytes.push(255, marker, (data.length + 2) >> 8, (data.length + 2) & 255, ...data);
    segment(0xe0, [...Array.from(new TextEncoder().encode('JFIF\0')), 1, 1, 0, 0, 1, 0, 1, 0, 0]);
    segment(0xc0, [8, 0, 20, 0, 40, 1, 1, 17, 0]);
    segment(0xda, [1, 1, 0, 0, 63, 0]);
    const scanStart = bytes.length;
    const header = Uint8Array.from(bytes);
    const result = new Uint8Array(header.length + scanSize + afterScan.length + 2);
    result.set(header);
    result.fill(33, scanStart, scanStart + scanSize);
    result.set(afterScan, scanStart + scanSize);
    result.set([255, 217], result.length - 2);
    return {bytes: result, scanStart};
}

test('EXIF-free JPEG retains complete marker structure and EOI, not scan pixels', async () => {
    const {bytes, scanStart} = plainJpeg();
    const {copy, result} = await rebuild(bytes);
    expect(result.kind).toBe('jpeg-header-ranges');
    expect(result.jpegPassthrough).toEqual({width: 40, height: 20});
    expect(result.chunks.map(chunk => ({offset: chunk.offset, length: chunk.bytes.length}))).toEqual([
        {offset: 0, length: scanStart}, {offset: bytes.length - 2, length: 2},
    ]);
    expect(copy.slice(0, scanStart)).toEqual(bytes.slice(0, scanStart));
    expect(copy.slice(scanStart, -2)).toEqual(new Uint8Array(bytes.length - scanStart - 2));
    expect(Array.from(copy.slice(-2))).toEqual([255, 217]);
});

test('JPEG retains metadata after scans instead of claiming absence from its first header', async () => {
    const afterScan = [255, 225, 0, 8, 69, 120, 105, 102, 0, 0, 255, 218, 0, 8, 1, 1, 0, 0, 63, 0, 45, 45];
    const {bytes, scanStart} = plainJpeg(100, afterScan);
    const {copy, result} = await rebuild(bytes);
    expect(copy.slice(scanStart + 100, scanStart + 100 + 20)).toEqual(bytes.slice(scanStart + 100, scanStart + 100 + 20));
    expect(result.jpegPassthrough).toBeNull();
});

test('progressive JPEG preserves every scan header and inter-scan tables', async () => {
    const afterScan = [255, 196, 0, 4, 0, 0, 255, 218, 0, 8, 1, 1, 0, 1, 63, 0, 45, 45];
    const {bytes, scanStart} = plainJpeg(100, afterScan);
    bytes[21] = 0xc2;
    const {copy, result} = await rebuild(bytes);
    expect(copy.slice(scanStart + 100, scanStart + 116)).toEqual(bytes.slice(scanStart + 100, scanStart + 116));
    expect(result.chunks).toHaveLength(3);
});

test.each([65535, 65536, 65537])('JPEG entropy marker handling crosses chunk boundary %i', async scanSize => {
    const {bytes, scanStart} = plainJpeg(scanSize);
    bytes.set([255, 0, 255, 208, 255, 255, 0], scanStart + 10);
    const {result} = await rebuild(bytes);
    expect(result.chunks.at(-1)?.offset).toBe(bytes.length - 2);
});

test.each(['missing-eoi', 'trailer', 'segment-length'])('JPEG absence walk rejects %s', async fault => {
    const {bytes} = plainJpeg();
    if (fault === 'missing-eoi') bytes.fill(44, bytes.length - 2);
    const input = fault === 'trailer' ? new Uint8Array([...Array.from(bytes), 0])
        : fault === 'segment-length' ? new Uint8Array([...Array.from(bytes.slice(0, -2)), 255, 254, 0, 1, 255, 217]) : bytes;
    await expect(rebuild(input)).rejects.toThrow('JPEG');
});

test('JPEG complete scan remains bounded by the metadata read budget', async () => {
    await expect(rebuild(plainJpeg(10000).bytes, 512)).rejects.toThrow('budget');
});

test.each([0xe1, 0xe2, 0xec, 0xed, 0xee, 0xfe])('metadata JPEG marker %i retains the full walk without enabling preview passthrough', async marker => {
    const {bytes, scanStart} = plainJpeg(800);
    const segment = new Uint8Array([255, marker, 0, 6, 1, 2, 3, 4]);
    const source = new Uint8Array([...Array.from(bytes.slice(0, 2)), ...Array.from(segment), ...Array.from(bytes.slice(2))]);
    const {copy, result} = await rebuild(source);
    expect(copy.slice(2, 10)).toEqual(segment);
    expect(result.chunks.at(-1)?.offset).toBe(source.length - 2);
    expect(copy.slice(scanStart + segment.length, -2)).toEqual(new Uint8Array(800));
    expect(result.jpegPassthrough).toBeNull();
});

test('large metadata-bearing JPEG keeps bounded legacy header transport instead of an extra complete scan', async () => {
    const {bytes} = plainJpeg(10000);
    const segment = [255, 225, 0, 8, 69, 120, 105, 102, 0, 0];
    const source = new Uint8Array([...Array.from(bytes.slice(0, 2)), ...segment, ...Array.from(bytes.slice(2))]);
    const {result} = await rebuild(source, 512);
    expect(result.chunks).toHaveLength(1);
    expect(result.chunks[0].offset).toBe(0);
    expect(result.readBytes).toBeLessThan(1024);
    expect(result.jpegPassthrough).toBeNull();
});

test('camera JPEG larger than packet payload can retain complete markers within its read budget', async () => {
    const {bytes} = plainJpeg(10000);
    const segment = [255, 225, 0, 8, 69, 120, 105, 102, 0, 0];
    const source = new Uint8Array([...Array.from(bytes.slice(0, 2)), ...segment, ...Array.from(bytes.slice(2))]);
    const {result} = await rebuild(source, 4096);
    expect(result.chunks.at(-1)?.offset).toBe(source.length - 2);
    expect(result.payloadBytes).toBeLessThan(4096);
    expect(result.readBytes).toBeLessThan(4096 * 8);
    expect(result.jpegPassthrough).toBeNull();
});

function mpfJpeg(imageCount = 2, little = false, scanSize = 800, child?: Uint8Array) {
    const first = plainJpeg(scanSize), children = Array.from({length: imageCount - 1}, () => child || plainJpeg(scanSize).bytes);
    const body = new Uint8Array(54 + imageCount * 16), view = new DataView(body.buffer);
    body.set(new TextEncoder().encode(little ? 'MPF\0II' : 'MPF\0MM'));
    view.setUint16(6, 42, little); view.setUint32(8, 8, little);
    ifd(view, 12, [[0xb000, 7, 4, little ? 0x30303130 : 0x30313030],
        [0xb001, 4, 1, imageCount], [0xb002, 7, imageCount * 16, 50]], little);
    const segment = new Uint8Array(body.length + 4);
    segment.set([255, 226]); new DataView(segment.buffer).setUint16(2, body.length + 2);
    const firstLength = first.bytes.length + segment.length, tiffBase = 10;
    const extents = [{offset: 0, length: firstLength, scanStart: first.scanStart + segment.length}];
    let offset = firstLength;
    for (const bytes of children) {
        extents.push({offset, length: bytes.length, scanStart: offset + plainJpeg(scanSize).scanStart});
        offset += bytes.length;
    }
    extents.forEach((image, i) => {
        const pos = 54 + i * 16;
        view.setUint32(pos, i === 0 ? 0x030000 : 0, little);
        view.setUint32(pos + 4, image.length, little);
        view.setUint32(pos + 8, i === 0 ? 0 : image.offset - tiffBase, little);
    });
    segment.set(body, 4);
    const bytes = new Uint8Array(offset);
    bytes.set(first.bytes.subarray(0, 2)); bytes.set(segment, 2); bytes.set(first.bytes.subarray(2), 2 + segment.length);
    children.forEach((child, i) => bytes.set(child, extents[i + 1].offset));
    return {bytes, view: new DataView(bytes.buffer), extents, entries: 60, tiffBase};
}

test.each([false, true])('MPF retains each JPEG metadata header and EOI without scan pixels (little=%s)', async little => {
    const {bytes, extents} = mpfJpeg(3, little);
    const {copy, result} = await rebuild(bytes);
    for (const image of extents) {
        expect(copy.slice(image.offset, image.scanStart)).toEqual(bytes.slice(image.offset, image.scanStart));
        expect(copy.slice(image.scanStart, image.offset + image.length - 2)).toEqual(new Uint8Array(800));
        expect(Array.from(copy.slice(image.offset + image.length - 2, image.offset + image.length))).toEqual([255, 217]);
    }
    expect(result.jpegPassthrough).toBeNull();
    expect(result.chunks).toHaveLength(4);
    expect(result.payloadBytes).toBe(bytes.length - 3 * 800);
    expect(result.jpegStructureFailure).toBeNull();
});

test('MPF child JPEG preserves progressive headers and metadata after scans', async () => {
    const after = [255, 254, 0, 6, 77, 69, 84, 65, 255, 218, 0, 8, 1, 1, 0, 1, 63, 0, 33, 33];
    const child = plainJpeg(800, after); child.bytes[21] = 0xc2;
    const {bytes, extents} = mpfJpeg(2, false, 800, child.bytes);
    const {copy, result} = await rebuild(bytes);
    const metadata = extents[1].offset + child.scanStart + 800;
    expect(copy.slice(metadata, metadata + 18)).toEqual(bytes.slice(metadata, metadata + 18));
    expect(copy.slice(extents[1].offset + child.scanStart, metadata)).toEqual(new Uint8Array(800));
    expect(Array.from(copy.slice(-2))).toEqual([255, 217]);
    expect(result.jpegPassthrough).toBeNull();
});

test('MPF supports 32 images within the shared metadata budget', async () => {
    const {bytes, extents} = mpfJpeg(32);
    const {copy, result} = await rebuild(bytes);
    expect(extents.every(image => copy[image.offset + image.length - 2] === 255 && copy[image.offset + image.length - 1] === 217)).toBe(true);
    expect(result.readBytes).toBeLessThan(128 * 1024);
    await expect(rebuild(mpfJpeg(32, false, 4).bytes, 1024)).rejects.toThrow('budget');
});

test.each(['magic', 'ifd-offset', 'ifd-cycle', 'ifd-overlap', 'ifd-count', 'image-count', 'entry-type',
    'entry-size', 'entry-offset', 'duplicate-count', 'first-offset', 'first-length', 'child-format',
    'child-offset', 'child-length', 'duplicate-child', 'dependency', 'child-soi', 'child-eoi', 'primary-eoi', 'child-frame', 'primary-frame',
    'extra-tail', 'too-many'])('unproven MPF %s retains only the legacy fallback', async fault => {
    let {bytes, view, extents, entries} = mpfJpeg(3);
    if (fault === 'magic') view.setUint16(12, 43);
    if (fault === 'ifd-offset') view.setUint32(14, 0xffffffff);
    if (fault === 'ifd-cycle') view.setUint32(56, 8);
    if (fault === 'ifd-overlap') view.setUint32(52, 8);
    if (fault === 'ifd-count') view.setUint16(18, 4097);
    if (fault === 'image-count') view.setUint32(40, 4);
    if (fault === 'entry-type') view.setUint16(46, 4);
    if (fault === 'entry-size') view.setUint32(48, 31);
    if (fault === 'entry-offset') view.setUint32(52, 0xffffffff);
    if (fault === 'duplicate-count') view.setUint16(20, 0xb001);
    if (fault === 'first-offset') view.setUint32(entries + 8, 1);
    if (fault === 'first-length') view.setUint32(entries + 4, extents[0].length - 1);
    if (fault === 'child-format') view.setUint32(entries + 16, 0x01000000);
    if (fault === 'child-offset') view.setUint32(entries + 24, 0xffffffff);
    if (fault === 'child-length') view.setUint32(entries + 20, bytes.length);
    if (fault === 'duplicate-child') view.setUint32(entries + 40, view.getUint32(entries + 24));
    if (fault === 'dependency') view.setUint16(entries + 12, 4);
    if (fault === 'child-soi') bytes[extents[1].offset + 1] = 0;
    if (fault === 'child-eoi') bytes[extents[1].offset + extents[1].length - 1] = 0;
    if (fault === 'primary-eoi') bytes[extents[0].length - 1] = 0;
    if (fault === 'child-frame') bytes[extents[1].offset + 21] = 0xfe;
    if (fault === 'primary-frame') bytes[extents[0].scanStart - 22] = 0xfe;
    if (fault === 'extra-tail') bytes = new Uint8Array([...Array.from(bytes), 0]);
    if (fault === 'too-many') ({bytes} = mpfJpeg(33));
    const {copy, result} = await rebuild(bytes);
    expect(result.chunks).toHaveLength(1);
    expect(copy.slice(-2)).toEqual(new Uint8Array(2));
    expect(result.jpegPassthrough).toBeNull();
    expect(result.jpegStructureFailure).toMatch(/MPF|JPEG/);
});

test('MPF nested indexes do not recursively expand the image traversal', async () => {
    const {bytes} = mpfJpeg(2, false, 800, mpfJpeg().bytes);
    const {result} = await rebuild(bytes);
    expect(result.chunks).toHaveLength(1);
    expect(result.jpegPassthrough).toBeNull();
});

test('MPF child attribute-only TIFF is retained without requiring a nested index', async () => {
    const child = plainJpeg(800), body = new Uint8Array(30), view = new DataView(body.buffer);
    body.set(new TextEncoder().encode('MPF\0II'));
    view.setUint16(6, 42, true); view.setUint32(8, 8, true);
    ifd(view, 12, [[0xb101, 4, 1, 2]]);
    const segment = new Uint8Array(34); segment.set([255, 226, 0, 32]); segment.set(body, 4);
    const withAttribute = new Uint8Array(child.bytes.length + segment.length);
    withAttribute.set(child.bytes.subarray(0, 2)); withAttribute.set(segment, 2); withAttribute.set(child.bytes.subarray(2), 36);
    const {bytes, extents} = mpfJpeg(2, false, 800, withAttribute);
    const {copy} = await rebuild(bytes);
    expect(copy.slice(extents[1].offset, extents[1].offset + 36)).toEqual(bytes.slice(extents[1].offset, extents[1].offset + 36));
    expect(Array.from(copy.slice(-2))).toEqual([255, 217]);
});

function webp(imageSize = 600, lossless = false) {
    const parts: Uint8Array[] = [], offsets: Record<string, number> = {};
    let pos = 12;
    const chunk = (type: string, data: Uint8Array) => {
        offsets[type] = pos;
        const bytes = new Uint8Array(8 + data.length + (data.length & 1));
        bytes.set(new TextEncoder().encode(type));
        new DataView(bytes.buffer).setUint32(4, data.length, true);
        bytes.set(data, 8); parts.push(bytes); pos += bytes.length;
    };
    const extended = new Uint8Array(10); extended[0] = 8; extended[4] = 39; extended[7] = 19;
    chunk('VP8X', extended);
    const image = new Uint8Array(imageSize).fill(77);
    if (lossless) {
        image[0] = 0x2f;
        new DataView(image.buffer).setUint32(1, 39 | (19 << 14), true);
    } else image.set([0, 0, 0, 0x9d, 1, 0x2a, 40, 0, 20, 0]);
    chunk(lossless ? 'VP8L' : 'VP8 ', image);
    chunk('EXIF', new TextEncoder().encode('Exif\0\0preserved-metadata'));
    const bytes = new Uint8Array(pos);
    bytes.set(new TextEncoder().encode('RIFF')); bytes.set(new TextEncoder().encode('WEBP'), 8);
    new DataView(bytes.buffer).setUint32(4, pos - 8, true);
    let cursor = 12;
    for (const part of parts) {bytes.set(part, cursor); cursor += part.length;}
    return {bytes, offsets};
}

test.each([false, true])('WebP retains metadata after image data and image prefixes, not pixels (lossless=%s)', async lossless => {
    const {bytes, offsets} = webp(601, lossless);
    const {copy, result} = await rebuild(bytes);
    const start = offsets[lossless ? 'VP8L' : 'VP8 '] + 8 + (lossless ? 5 : 10);
    expect(result.kind).toBe('webp-chunk-ranges');
    expect(copy.slice(0, start)).toEqual(bytes.slice(0, start));
    expect(copy.slice(start, offsets.EXIF - 1)).toEqual(new Uint8Array(offsets.EXIF - 1 - start));
    expect(copy.slice(offsets.EXIF)).toEqual(bytes.slice(offsets.EXIF));
    expect(result.jpegPassthrough).toBeNull();
});

test.each(['size', 'trailer', 'animation', 'reserved', 'duplicate', 'length', 'missing-image', 'unknown'])('WebP rejects %s', async fault => {
    const {bytes, offsets} = webp(), view = new DataView(bytes.buffer);
    if (fault === 'size') view.setUint32(4, bytes.length, true);
    if (fault === 'animation') bytes[offsets.VP8X + 8] |= 2;
    if (fault === 'reserved') bytes[offsets.VP8X + 9] = 1;
    if (fault === 'duplicate') bytes.set(new TextEncoder().encode('VP8X'), offsets.EXIF);
    if (fault === 'length') view.setUint32(offsets['VP8 '] + 4, 0xffffffff, true);
    if (fault === 'missing-image') bytes.set(new TextEncoder().encode('XMP '), offsets['VP8 ']);
    if (fault === 'unknown') bytes.set(new TextEncoder().encode('ANMF'), offsets['VP8 ']);
    await expect(rebuild(fault === 'trailer' ? new Uint8Array([...Array.from(bytes), 0]) : bytes)).rejects.toThrow('WebP');
});

test('WebP skips large image payloads within the metadata read budget', async () => {
    const {bytes} = webp(1024 * 1024);
    const {result} = await rebuild(bytes, 512);
    expect(result.payloadBytes).toBeLessThan(512);
    expect(result.readBytes).toBeLessThan(1024);
});

function png() {
    const bytes = new Uint8Array(1024), view = new DataView(bytes.buffer);
    bytes.set([137, 80, 78, 71, 13, 10, 26, 10]);
    const offsets: Record<string, number> = {};
    let pos = 8;
    const chunk = (type: string, payload: Uint8Array) => {
        offsets[type] = pos;
        view.setUint32(pos, payload.length);
        bytes.set(new TextEncoder().encode(type), pos + 4);
        bytes.set(payload, pos + 8);
        bytes.fill(17, pos + 8 + payload.length, pos + 12 + payload.length);
        pos += payload.length + 12;
    };
    const header = new Uint8Array(13), hv = new DataView(header.buffer);
    hv.setUint32(0, 900); hv.setUint32(4, 705); header[8] = 8; header[9] = 2;
    chunk('IHDR', header);
    chunk('iCCP', new Uint8Array(20).fill(77));
    chunk('IDAT', new Uint8Array(512).fill(99));
    chunk('eXIf', new Uint8Array(24).fill(66));
    chunk('iTXt', new TextEncoder().encode('Comment\0text'));
    chunk('IEND', new Uint8Array());
    return {bytes: bytes.slice(0, pos), offsets};
}

test('PNG retains metadata before and after IDAT, structural headers and CRCs, but no pixels', async () => {
    const {bytes, offsets} = png();
    const {copy, result} = await rebuild(bytes);
    expect(result.kind).toBe('png-chunk-ranges');
    expect(copy.slice(0, offsets.IDAT + 8)).toEqual(bytes.slice(0, offsets.IDAT + 8));
    expect(copy.slice(offsets.IDAT + 8, offsets.IDAT + 8 + 512)).toEqual(new Uint8Array(512));
    expect(copy.slice(offsets.IDAT + 8 + 512)).toEqual(bytes.slice(offsets.IDAT + 8 + 512));
    expect(result.payloadBytes).toBe(bytes.length - 512);
});

test.each(['header', 'duplicate', 'critical', 'reserved', 'length', 'end', 'missing-data', 'trailer', 'animation'])('PNG rejects malformed/unsupported %s', async fault => {
    const {bytes, offsets} = png(), view = new DataView(bytes.buffer);
    if (fault === 'header') view.setUint32(8, 12);
    if (fault === 'duplicate') bytes.set(new TextEncoder().encode('IHDR'), offsets.iCCP + 4);
    if (fault === 'critical') bytes.set(new TextEncoder().encode('ABCD'), offsets.iCCP + 4);
    if (fault === 'reserved') bytes.set(new TextEncoder().encode('iCcP'), offsets.iCCP + 4);
    if (fault === 'length') view.setUint32(offsets.IDAT, 0xffffffff);
    if (fault === 'end') bytes.set(new TextEncoder().encode('iTXt'), offsets.IEND + 4);
    if (fault === 'missing-data') bytes.set(new TextEncoder().encode('tEXt'), offsets.IDAT + 4);
    if (fault === 'animation') bytes.set(new TextEncoder().encode('fdAT'), offsets.IDAT + 4);
    const input = fault === 'trailer' ? new Uint8Array([...Array.from(bytes), 0]) : bytes;
    await expect(rebuild(input)).rejects.toThrow('PNG');
});

test('PNG respects metadata budgets without charging compressed image payload', async () => {
    const {bytes} = png();
    await expect(rebuild(bytes, 256)).resolves.toBeDefined();
    await expect(rebuild(bytes, 100)).rejects.toThrow('budget');
    await expect(rebuild(bytes.slice(0, -1))).rejects.toThrow('PNG');
});

function heic(locationVersion = 1, method = 0, type = 'Exif', entryVersion = 2) {
    const encoder = new TextEncoder();
    const box = (type: string, payload: Uint8Array) => {
        const bytes = new Uint8Array(8 + payload.length);
        new DataView(bytes.buffer).setUint32(0, bytes.length);
        bytes.set(encoder.encode(type), 4);
        bytes.set(payload, 8);
        return bytes;
    };
    const join = (...chunks: Uint8Array[]) => {
        const bytes = new Uint8Array(chunks.reduce((sum, chunk) => sum + chunk.length, 0));
        let offset = 0;
        chunks.forEach(chunk => {bytes.set(chunk, offset); offset += chunk.length;});
        return bytes;
    };
    const entry = (id: number, type: string) => {
        const prefix = new Uint8Array(entryVersion === 3 ? 10 : 8);
        const view = new DataView(prefix.buffer);
        prefix[0] = entryVersion;
        if (entryVersion === 3) view.setUint32(4, id);
        else view.setUint16(4, id);
        const strings = entryVersion < 2 ? encoder.encode('item\0application/rdf+xml\0\0')
            : encoder.encode(type + '\0' + (type === 'mime' ? 'application/rdf+xml\0\0' : ''));
        return box('infe', join(prefix, strings));
    };
    const infoPrefix = new Uint8Array(6);
    new DataView(infoPrefix.buffer).setUint16(4, 2);
    const info = box('iinf', join(infoPrefix, entry(1, type), entry(2, 'hvc1')));
    const location = new Uint8Array(locationVersion === 2 ? 54 : locationVersion === 1 ? 48 : 44);
    const lv = new DataView(location.buffer);
    location[0] = locationVersion;
    lv.setUint16(4, 0x4440);
    if (locationVersion === 2) lv.setUint32(6, 2);
    else lv.setUint16(6, 2);
    let pos = locationVersion === 2 ? 10 : 8;
    const positions: {method?: number; reference: number; base: number; offset: number; length: number; count: number}[] = [];
    for (let id = 1; id <= 2; id++) {
        if (locationVersion === 2) {lv.setUint32(pos, id); pos += 4;}
        else {lv.setUint16(pos, id); pos += 2;}
        const methodPos = locationVersion ? pos : undefined;
        if (locationVersion) {lv.setUint16(pos, id === 1 ? method : 0); pos += 2;}
        const reference = pos; pos += 2;
        const base = pos; pos += 4;
        const count = pos; lv.setUint16(pos, 1); pos += 2;
        const offset = pos; lv.setUint32(pos, id === 1 ? (method === 1 ? 4 : 2052) : 512); pos += 4;
        const length = pos; lv.setUint32(pos, id === 1 ? 16 : 512); pos += 4;
        positions.push({method: methodPos, reference, base, offset, length, count});
    }
    const idat = new Uint8Array(20);
    idat.fill(77, 4);
    const meta = box('meta', join(new Uint8Array(4), info, box('iloc', location), ...(method === 1 ? [box('idat', idat)] : [])));
    const bytes = new Uint8Array(4096);
    bytes.set(box('ftyp', encoder.encode('heic\0\0\0\0')));
    bytes.set(meta, 16);
    const media = 16 + meta.length;
    new DataView(bytes.buffer).setUint32(media, bytes.length - media);
    bytes.set(encoder.encode('mdat'), media + 4);
    bytes.fill(99, 512, 1024);
    bytes.fill(77, 2052, 2068);
    const locationBase = 16 + 8 + 4 + info.length + 8;
    return {bytes, view: new DataView(bytes.buffer), locationBase, positions,
        idatStart: method === 1 ? 16 + meta.length - idat.length : undefined};
}

test.each([0, 1, 2])('HEIC iloc version %i retains metadata extents, not image data', async version => {
    const {bytes} = heic(version);
    const {copy} = await rebuild(bytes);
    expect(copy.slice(2052, 2068)).toEqual(bytes.slice(2052, 2068));
    expect(copy.slice(512, 1024)).toEqual(new Uint8Array(512));
});

test.each(['Exif', 'mime', 'uri '])('HEIC retains %s items', async type => {
    const {bytes} = heic(2, 0, type, 3);
    expect((await rebuild(bytes)).copy.slice(2052, 2068)).toEqual(bytes.slice(2052, 2068));
});

test.each([0, 1])('HEIC legacy infe version %i retains declared MIME data', async version => {
    const {bytes} = heic(0, 0, 'mime', version);
    expect((await rebuild(bytes)).copy.slice(2052, 2068)).toEqual(bytes.slice(2052, 2068));
});

test('HEIC idat construction retains bounded metadata', async () => {
    const {bytes, idatStart} = heic(1, 1);
    expect((await rebuild(bytes)).copy.slice(idatStart! + 4, idatStart! + 20)).toEqual(new Uint8Array(16).fill(77));
});

test.each(['method', 'reference', 'offset', 'length', 'count', 'size', 'version', 'id', 'unsafe'])('invalid HEIC %s rejects incomplete packets', async fault => {
    const {bytes, view, locationBase, positions} = heic();
    const item = positions[0];
    if (fault === 'method') view.setUint16(locationBase + item.method!, 2);
    if (fault === 'reference') view.setUint16(locationBase + item.reference, 1);
    if (fault === 'offset') view.setUint32(locationBase + item.offset, bytes.length);
    if (fault === 'length') view.setUint32(locationBase + item.length, 0);
    if (fault === 'count') view.setUint16(locationBase + item.count, 4097);
    if (fault === 'size') bytes[locationBase + 4] = 0xf4;
    if (fault === 'version') bytes[locationBase] = 3;
    if (fault === 'id') view.setUint16(locationBase + 8, 3);
    if (fault === 'unsafe') {
        bytes[locationBase + 5] = 0x80;
        bytes.fill(255, locationBase + item.base, locationBase + item.base + 8);
    }
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete metadata traversal');
});

test('HEIC idat references cannot escape their container; metadata retains budget limits', async () => {
    const {bytes, view, locationBase, positions} = heic(1, 1);
    view.setUint32(locationBase + positions[0].offset, 19);
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete metadata traversal');
    await expect(rebuild(heic().bytes, 100)).rejects.toThrow('budget');
});

test('HEIF JPEG items retain headers without copying compressed preview pixels', async () => {
    const {bytes, view, locationBase, positions} = heic(1, 0, 'jpeg');
    view.setUint32(locationBase + positions[0].length, 512);
    bytes.set([255, 216, 255, 225, 0, 4, 69, 88, 255, 218, 0, 2], 2052);
    bytes.fill(99, 2064, 2564);
    const {copy} = await rebuild(bytes);
    expect(copy.slice(2052, 2064)).toEqual(bytes.slice(2052, 2064));
    expect(copy.slice(2064, 2564)).toEqual(new Uint8Array(500));
    view.setUint16(locationBase + positions[0].count, 2);
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete metadata traversal');
});

function samsungTrailer() {
    const bytes = new Uint8Array(4096), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    bytes.fill(99, 512, 3000);
    bytes.set(new TextEncoder().encode('SEFH'), 4064);
    view.setUint32(4068, 101, true);
    view.setUint32(4072, 1, true);
    view.setUint16(4078, 0x0a01, true);
    view.setUint32(4080, 164, true);
    view.setUint32(4084, 20, true);
    view.setUint32(4088, 24, true);
    bytes.set(new TextEncoder().encode('SEFT'), 4092);
    view.setUint16(3902, 0x0a01, true);
    view.setUint32(3904, 4, true);
    bytes.set(new TextEncoder().encode('UTC\0'), 3908);
    bytes.fill(77, 3912, 3920);
    return {bytes, view};
}

test('Samsung SEFT trailer metadata survives without retaining sensor data', async () => {
    const {bytes} = samsungTrailer();
    const {copy} = await rebuild(bytes);
    expect(copy.slice(3900, 3920)).toEqual(bytes.slice(3900, 3920));
    expect(copy.slice(4064)).toEqual(bytes.slice(4064));
    expect(copy.slice(512, 3000)).toEqual(new Uint8Array(2488));
});

test('Samsung motion JPEG preserves the original end marker before its trailer', async () => {
    const {bytes, view} = samsungTrailer();
    view.setUint16(4078, 0x0a30, true);
    bytes.set([255, 216, 255, 225, 0, 4, 69, 88, 255, 218, 0, 2]);
    bytes.set([255, 217], 3898);
    const {copy} = await rebuild(bytes);
    expect(copy.slice(3898)).toEqual(bytes.slice(3898));
    expect(copy.slice(512, 3000)).toEqual(new Uint8Array(2488));
    bytes[3899] = 0;
    await expect(rebuild(bytes)).rejects.toThrow('Samsung JPEG trailer boundary');
});

test('Samsung JPEG metadata-only trailers do not require a motion-photo end boundary', async () => {
    const {bytes} = samsungTrailer();
    bytes.set([255, 216, 255, 225, 0, 4, 69, 88, 255, 218, 0, 2]);
    expect((await rebuild(bytes)).copy.slice(3900)).toEqual(bytes.slice(3900));
});

test.each(['length', 'header', 'count', 'offset', 'size', 'name'])('invalid Samsung trailer %s rejects packets', async fault => {
    const {bytes, view} = samsungTrailer();
    if (fault === 'length') view.setUint32(4088, 65536, true);
    if (fault === 'header') bytes[4064] = 0;
    if (fault === 'count') view.setUint32(4072, 4097, true);
    if (fault === 'offset') view.setUint32(4080, 5000, true);
    if (fault === 'size') view.setUint32(4084, 165, true);
    if (fault === 'name') view.setUint32(3904, 20, true);
    await expect(rebuild(bytes)).rejects.toThrow('Samsung trailer');
});

test('Samsung trailer keeps metadata budgets and unknown QDIO fallback', async () => {
    await expect(rebuild(samsungTrailer().bytes, 16)).rejects.toThrow('budget');
    const {bytes} = samsungTrailer();
    bytes.set(new TextEncoder().encode('QDIOBS'), 4090);
    await expect(rebuild(bytes)).rejects.toThrow('Unsupported Samsung trailer');
});
function canon(version = 2, little = true) {
    const bytes = new Uint8Array(4096);
    const view = new DataView(bytes.buffer);
    bytes.set(little ? [73, 73, 42, 0, 8, 0, 0, 0] : [77, 77, 0, 42, 0, 0, 0, 8]);
    bytes.set([67, 82, 2, 0], 8);
    // The CR2 TIFF directory lives after its 16-byte header.
    view.setUint32(4, 16, little);
    ifd(view, 16, [[0x010f, 2, 6, 400], [0x8769, 4, 1, 448]], little);
    bytes.set(new TextEncoder().encode('Canon\0'), 400);
    ifd(view, 448, [[0x927c, 7, 18, 512]], little);
    ifd(view, 512, [[0x0083, 4, 1, 3072]], little);
    bytes.fill(99, 800, 2000);
    view.setUint32(3072, 0xffffffff);
    view.setUint32(3076, version, little);
    if (version === 1 || version === 2) {
        view.setUint32(3100, 2, little);
        bytes.fill(71, 3104, 3168);
    } else if (version === 3) {
        view.setUint32(3080, 20, little);
        bytes.fill(72, 3084, 3104);
        view.setUint32(3104, 20, little);
        bytes.fill(73, 3108, 3128);
        view.setUint32(3128, 32, little);
        bytes.fill(74, 3132, 3160);
    }
    return {bytes, view};
}

test.each([[1, false], [2, true], [3, true], [3, false]])('Canon decision version %i preserves metadata but not sensor bytes', async (version, little) => {
    const {bytes} = canon(version as number, little as boolean);
    const {copy} = await rebuild(bytes);
    const end = version === 3 ? 3160 : 3168;
    expect(copy.slice(3072, end)).toEqual(bytes.slice(3072, end));
    expect(copy.slice(800, 2000)).toEqual(new Uint8Array(1200));
});

test.each(['signature', 'version', 'count', 'pointer', 'length', 'type'])('invalid Canon %s cannot yield a packet', async fault => {
    const {bytes, view} = canon(fault === 'length' ? 3 : 2);
    if (fault === 'signature') view.setUint32(3072, 0);
    if (fault === 'version') view.setUint32(3076, 99, true);
    if (fault === 'count') view.setUint32(3100, 20, true);
    if (fault === 'pointer') view.setUint32(522, bytes.length + 1, true);
    if (fault === 'length') view.setUint32(3128, 0x10005, true);
    if (fault === 'type') view.setUint16(516, 3, true);
    await expect(rebuild(bytes)).rejects.toThrow();
});

test('Canon extension requires CR2 and Canon signatures and keeps the packet budget', async () => {
    const {bytes} = canon();
    bytes.set(new TextEncoder().encode('Nikon\0'), 400);
    expect((await rebuild(bytes)).copy[3072]).toBe(0);
    bytes.set(new TextEncoder().encode('Canon\0'), 400);
    bytes[8] = 0;
    expect((await rebuild(bytes)).copy[3072]).toBe(0);
    bytes[8] = 67;
    await expect(rebuild(bytes, 300)).rejects.toThrow('budget');
});

test('Phase One retains metadata directories without retaining RawData', async () => {
    const bytes = new Uint8Array(10000), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    ifd(view, 8, [[0x927c, 7, 9488, 512]]);
    bytes.set(new TextEncoder().encode('IIIITwaR'), 512);
    view.setUint32(520, 12, true);
    view.setUint32(524, 2, true);
    [0x0102, 0x010f].forEach((tag, index) => {
        const p = 532 + index * 16;
        view.setUint32(p, tag, true);
        view.setUint32(p + 4, 1, true);
        view.setUint32(p + 8, index ? 7000 : 6, true);
        view.setUint32(p + 12, index ? 2000 : 100, true);
    });
    bytes.set(new TextEncoder().encode('ABC12\0'), 612);
    bytes.fill(9, 2512, 9512);
    const {copy, result} = await rebuild(bytes, 1024);
    expect(copy.slice(612, 618)).toEqual(bytes.slice(612, 618));
    expect(copy.slice(2512, 9512)).toEqual(new Uint8Array(7000));
    expect(result.payloadBytes).toBeLessThan(1024);
    view.setUint32(532 + 12, 10000, true);
    await expect(rebuild(bytes, 1024)).rejects.toThrow('Incomplete');
});

test('Panasonic oversized embedded JPEG retains headers without scan pixels', async () => {
    const bytes = new Uint8Array(10000), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 85, 0, 8, 0, 0, 0]);
    ifd(view, 8, [[0x0127, 7, 9000, 512]]);
    bytes.set([255, 216, 255, 225, 0, 4, 69, 88, 255, 218, 0, 2], 512);
    bytes.fill(88, 600, 9512);
    const {copy} = await rebuild(bytes, 1024);
    expect(copy.slice(512, 524)).toEqual(bytes.slice(512, 524));
    expect(copy.slice(600, 9512)).toEqual(new Uint8Array(8912));
});

test('Olympus subdirectory fields are not reinterpreted as JPEG pointers', async () => {
    const bytes = new Uint8Array(4096), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 82, 79, 8, 0, 0, 0]);
    ifd(view, 8, [[0x927c, 7, 128, 512]]);
    bytes.set(new TextEncoder().encode('OLYMPUS\0II'), 512);
    ifd(view, 524, [[0x2010, 13, 1, 128]]);
    ifd(view, 640, [[0x0201, 4, 1, 0xffffffff], [0x0202, 4, 1, 0xffffffff], [0x0100, 2, 6, 256]]);
    bytes.set(new TextEncoder().encode('MODEL\0'), 768);
    expect((await rebuild(bytes)).copy.slice(768, 774)).toEqual(bytes.slice(768, 774));
});

test('zero-filled IFD padding is ignored, but malformed unknown entries are rejected', async () => {
    const bytes = new Uint8Array(1024), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    ifd(view, 8, [[0, 0, 0, 0], [0x010f, 2, 6, 512]]);
    bytes.set(new TextEncoder().encode('TEST\0\0'), 512);
    expect((await rebuild(bytes)).copy.slice(512, 518)).toEqual(bytes.slice(512, 518));
    view.setUint32(14, 1, true);
    await expect(rebuild(bytes)).rejects.toThrow('Unsupported TIFF');
});

test('Mamiya bad linked IFD is tolerated only inside a fully retained MakerNote', async () => {
    const bytes = new Uint8Array(8192), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    const make = new TextEncoder().encode('Mamiya-OP Co.,Ltd.\0');
    const model = new TextEncoder().encode('MAMIYA ZD\0');
    ifd(view, 8, [[0x010f, 2, make.length, 400], [0x0110, 2, model.length, 430],
        [0x014a, 4, 1, 512], [0x8769, 4, 1, 448]]);
    bytes.set(make, 400); bytes.set(model, 430);
    ifd(view, 448, [[0x927c, 7, 1024, 1024]]);
    ifd(view, 512, [[0x0100, 4, 1, 10]]);
    view.setUint32(526, 1800, true);
    ifd(view, 1800, [[999, 999, 1, 0]]);
    expect((await rebuild(bytes)).copy.slice(1024, 2048)).toEqual(bytes.slice(1024, 2048));
    bytes[430] = 88;
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete');
    bytes[430] = 77;
    view.setUint32(454, 256, true);
    await expect(rebuild(bytes)).rejects.toThrow('Unsupported TIFF');
    view.setUint32(454, 1024, true);
    view.setUint16(1804, 16, true);
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete');
});

function samsungPreview() {
    const bytes = new Uint8Array(16384), view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    ifd(view, 8, [[0x010f, 2, 8, 300], [0x0110, 2, 8, 320], [0x8769, 4, 1, 400],
        [0x0111, 4, 1, 4096], [0x0117, 4, 1, 8192]]);
    bytes.set(new TextEncoder().encode('SAMSUNG\0'), 300);
    bytes.set(new TextEncoder().encode('NX mini\0'), 320);
    ifd(view, 400, [[0x927c, 7, 256, 512]]);
    ifd(view, 512, [[1, 7, 4, 0x30303130], [0x0035, 4, 1, 256]]);
    const preview = plainJpeg(256);
    ifd(view, 768, [[0x0103, 3, 1, 6], [0x011a, 5, 1, 600], [0x011b, 5, 1, 608],
        [0x0201, 4, 1, 1000], [0x0202, 4, 1, preview.bytes.length]]);
    bytes.set([72, 0, 0, 0, 1, 0, 0, 0, 96, 0, 0, 0, 1, 0, 0, 0], 1112);
    bytes.set(preview.bytes, 1512);
    bytes.fill(99, 4096, 12288);
    return {bytes, view, scanStart: 1512 + preview.scanStart};
}

test('Samsung external PreviewIFD retains relative resolutions and JPEG metadata without pixels', async () => {
    const {bytes, scanStart} = samsungPreview();
    const {copy, result} = await rebuild(bytes, 1024);
    expect(copy.slice(768, 834)).toEqual(bytes.slice(768, 834));
    expect(copy.slice(1112, 1128)).toEqual(bytes.slice(1112, 1128));
    expect(copy.slice(1512, scanStart)).toEqual(bytes.slice(1512, scanStart));
    expect(copy.slice(scanStart, scanStart + 256)).toEqual(new Uint8Array(256));
    expect(copy.slice(4096, 12288)).toEqual(new Uint8Array(8192));
    expect(result.payloadBytes).toBeLessThan(1024);
    expect(result.readBytes).toBeLessThan(2048);
});

test.each(['pointer-type', 'pointer-count', 'offset-base', 'directory-count', 'value-offset',
    'jpeg-type', 'jpeg-length', 'missing-jpeg', 'next-ifd', 'maker-count'])('Samsung external preview rejects %s', async fault => {
    const {bytes, view} = samsungPreview();
    if (fault === 'pointer-type') view.setUint16(528, 3, true);
    if (fault === 'pointer-count') view.setUint32(530, 2, true);
    if (fault === 'offset-base') view.setUint32(534, 0xffffffff, true);
    if (fault === 'directory-count') view.setUint16(768, 4097, true);
    if (fault === 'value-offset') view.setUint32(790, 0xffffffff, true);
    if (fault === 'jpeg-type') view.setUint16(808, 3, true);
    if (fault === 'jpeg-length') view.setUint32(826, bytes.length, true);
    if (fault === 'missing-jpeg') view.setUint32(814, 0, true);
    if (fault === 'next-ifd') view.setUint32(830, 0xffffffff, true);
    if (fault === 'maker-count') view.setUint16(512, 100, true);
    await expect(rebuild(bytes)).rejects.toThrow('Incomplete');
});

test('Samsung external preview stays within read and merged payload budgets', async () => {
    const {bytes, view} = samsungPreview();
    await expect(rebuild(bytes, 512)).rejects.toThrow('budget');
    view.setUint32(786, 1024, true);
    await expect(rebuild(bytes, 1024)).rejects.toThrow('Incomplete');
});

test('Samsung Type2 traversal requires its make and version signatures', async () => {
    const {bytes} = samsungPreview();
    bytes[300] = 88;
    expect((await rebuild(bytes)).copy.slice(768, 834)).toEqual(new Uint8Array(66));
    bytes[300] = 83; bytes[522] = 88;
    expect((await rebuild(bytes)).copy.slice(768, 834)).toEqual(new Uint8Array(66));
});

function kodakDirectories(little = true, legacy = true) {
    const bytes = new Uint8Array(16384), view = new DataView(bytes.buffer);
    bytes.set(little ? [73, 73, 42, 0, 8, 0, 0, 0] : [77, 77, 0, 42, 0, 0, 0, 8]);
    const make = new TextEncoder().encode('EASTMAN KODAK COMPANY\0');
    const model = new TextEncoder().encode('KODAK P712 ZOOM DIGITAL CAMERA\0');
    ifd(view, 8, [[0x010f, 2, make.length, 300], [0x0110, 2, model.length, 324],
        [0x8769, 4, 1, 400], [0xfe00, 4, 1, 2048], [0x0111, 4, 1, 4096], [0x0117, 4, 1, 8192]], little);
    bytes.set(make, 300); bytes.set(model, 324);
    ifd(view, 400, legacy ? [[0x927c, 7, 64, 512], [0xa005, 4, 1, 314]] : [], little);
    ifd(view, 512, [[0xff00, 4, 1, 768], [0x0f00, 7, 16, 1000]], little);
    ifd(view, 768, [[0xf900, 3, 1, little ? 3000 : 3000 << 16], [0xfa04, 2, 16, 1100]], little);
    bytes.set(new TextEncoder().encode('CAMERA-12345678\0\0'), 1100);
    bytes.fill(44, 1000, 1016);
    ifd(view, 2048, [[0xfa00, 2, 16, 2200], [0xfa0d, 1, 1, little ? 3 : 3 << 24],
        [0xfa25, 4, 3, 2300], [0xfa29, 4, 3, 2312]], little);
    bytes.set(new TextEncoder().encode('SERIAL-12345678\0\0'), 2200);
    for (let i = 0; i < 6; i++) view.setUint32(2300 + i * 4, 65536 + i * 100, little);
    bytes.fill(99, 4096, 12288);
    return {bytes, view};
}

test.each([true, false])('Kodak KDC_IFD retains serial and white balance values without sensor data (little=%s)', async little => {
    const {bytes} = kodakDirectories(little, false);
    const {copy, result} = await rebuild(bytes, 1024);
    expect(copy.slice(2048, 2102)).toEqual(bytes.slice(2048, 2102));
    expect(copy.slice(2200, 2216)).toEqual(bytes.slice(2200, 2216));
    expect(copy.slice(2300, 2324)).toEqual(bytes.slice(2300, 2324));
    expect(copy.slice(4096, 12288)).toEqual(new Uint8Array(8192));
    expect(result.payloadBytes).toBeLessThan(1024);
});

test('Kodak legacy absolute CameraInfo and invalid Interop prefix survive outside MakerNote bounds', async () => {
    const {bytes} = kodakDirectories();
    const {copy, result} = await rebuild(bytes, 1024);
    expect(copy.slice(314, 328)).toEqual(bytes.slice(314, 328));
    expect(copy.slice(768, 794)).toEqual(bytes.slice(768, 794));
    expect(copy.slice(1000, 1016)).toEqual(bytes.slice(1000, 1016));
    expect(copy.slice(1100, 1116)).toEqual(bytes.slice(1100, 1116));
    expect(copy.slice(4096, 12288)).toEqual(new Uint8Array(8192));
    expect(result.readBytes).toBeLessThan(2048);
});

test.each(['make', 'model', 'outside-make', 'other-overlap', 'model-layout', 'valid-format', 'camera-type', 'camera-offset',
    'camera-value', 'kdc-type', 'kdc-count', 'kdc-offset', 'kdc-entries', 'kdc-value'])('Kodak malformed/unknown profile rejects %s', async fault => {
    const {bytes, view} = kodakDirectories();
    if (fault === 'make') bytes[300] = 88;
    if (fault === 'model') bytes[330] = 88;
    if (fault === 'outside-make') view.setUint32(422, 12000, true);
    if (fault === 'other-overlap') view.setUint32(422, 316, true);
    if (fault === 'model-layout') {
        bytes.copyWithin(1500, 324, 354);
        view.setUint32(30, 1500, true);
    }
    if (fault === 'valid-format') view.setUint16(318, 3, true);
    if (fault === 'camera-type') view.setUint16(516, 3, true);
    if (fault === 'camera-offset') view.setUint32(522, 0xffffffff, true);
    if (fault === 'camera-value') view.setUint32(790, 0xffffffff, true);
    if (fault === 'kdc-type') view.setUint16(48, 3, true);
    if (fault === 'kdc-count') view.setUint32(50, 2, true);
    if (fault === 'kdc-offset') view.setUint32(54, 0xffffffff, true);
    if (fault === 'kdc-entries') view.setUint16(2048, 4097, true);
    if (fault === 'kdc-value') view.setUint32(2058, 0xffffffff, true);
    // With an unknown Make, the ordinary invalid Interop traversal still fails.
    await expect(rebuild(bytes)).rejects.toThrow();
});

test('Kodak external metadata remains subject to payload and value read budgets', async () => {
    const {bytes, view} = kodakDirectories();
    await expect(rebuild(bytes, 512)).rejects.toThrow('budget');
    view.setUint32(2054, 1025, true);
    await expect(rebuild(bytes, 1024)).rejects.toMatchObject({
        message: 'Incomplete metadata traversal',
        notices: expect.arrayContaining([expect.objectContaining({tag: 0xfa00, error: 'Metadata read budget exceeded'})]),
    });
});

test('Kodak private fields are not standard EXIF or JPEG directory pointers', async () => {
    const {bytes, view} = kodakDirectories();
    ifd(view, 768, [[0xa005, 4, 1, 0xffffffff], [0x0201, 4, 1, 0xffffffff], [0x0202, 4, 1, 0xffffffff]]);
    ifd(view, 2048, [[0x8769, 4, 1, 0xffffffff], [0x0201, 4, 1, 0xffffffff], [0x0202, 4, 1, 0xffffffff]]);
    view.setUint32(2086, 0, true);
    await expect(rebuild(bytes, 1024)).resolves.toBeDefined();
});

test('Samsung external preview cannot extend an IFD chain past the traversal budget', async () => {
    const {bytes, view} = samsungPreview();
    view.setUint32(830, 2048, true);
    for (let i = 0; i < 30; i++) {
        const pos = 512 + 2048 + i * 64;
        ifd(view, pos, [[0x0201, 4, 1, 1000], [0x0202, 4, 1, plainJpeg(256).bytes.length]]);
        view.setUint32(pos + 26, 2048 + (i + 1) * 64, true);
    }
    await expect(rebuild(bytes)).rejects.toMatchObject({
        notices: expect.arrayContaining([expect.objectContaining({error: 'IFD traversal limit'})]),
    });
});
