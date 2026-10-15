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
    const {copy} = await rebuild(bytes);
    expect(copy.slice(scanStart + 100, scanStart + 100 + 20)).toEqual(bytes.slice(scanStart + 100, scanStart + 100 + 20));
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
