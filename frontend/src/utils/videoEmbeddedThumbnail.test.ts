import {File as NodeFile} from 'node:buffer';
import {embeddedVideoJpegs, EMBEDDED_VIDEO_IMAGE_BYTES, EMBEDDED_VIDEO_READ_BYTES, EMBEDDED_VIDEO_MAX_READS} from './videoEmbeddedThumbnail';

const concat = (...parts: Uint8Array[]) => {
    const out = new Uint8Array(parts.reduce((total, bytes) => total + bytes.length, 0));
    let pos = 0;
    for (const part of parts) { out.set(part, pos); pos += part.length; }
    return out;
};
const text = (value: string) => new TextEncoder().encode(value);
const uuidBytes = (id: string) => new Uint8Array(id.match(/../g)!.map(byte => parseInt(byte, 16)));
const canon = '85c0b687820f11e08111f4ce462b6a48';
const preview = 'eaf42b5e1c984b88b9fbb7dc406e4d16';
const jpeg = (width = 160, height = 120) => new Uint8Array([
    255, 216, 255, 192, 0, 11, 8, height >> 8, height & 255, width >> 8, width & 255, 1, 1, 17, 0, 255, 217,
]);
const box = (type: string, bytes: Uint8Array, extended = false) => {
    const header = new Uint8Array(extended ? 16 : 8), view = new DataView(header.buffer);
    view.setUint32(0, extended ? 1 : bytes.length + 8);
    header.set(text(type), 4);
    if (extended) view.setBigUint64(8, BigInt(bytes.length + 16));
    return concat(header, bytes);
};
const ftyp = (brand = 'crx ') => box('ftyp', concat(text(brand), new Uint8Array(4)));
const thmb = (bytes: Uint8Array, declared = bytes.length) => {
    const prefix = new Uint8Array(16);
    new DataView(prefix.buffer).setUint32(8, declared);
    return box('THMB', concat(prefix, bytes, new Uint8Array(5)));
};
const camera = (bytes: Uint8Array) => box('moov', box('uuid', concat(uuidBytes(canon), bytes)));
const nikon = (...parts: Uint8Array[]) => box('moov', box('udta', box('NCDT', concat(...parts))));
const file = (bytes: Uint8Array, name = 'clip.crm') => new NodeFile([bytes], name) as unknown as File;
const collect = async (source: File, signal = new AbortController().signal) => {
    const rows = [];
    for await (const row of embeddedVideoJpegs(source, signal, 40_000_000)) rows.push(row);
    return rows;
};

test('extracts only the declared Canon JPEG, not padding or video samples', async () => {
    const picture = jpeg(), movie = box('mdat', new Uint8Array(1024 * 1024).fill(165));
    const source = file(concat(ftyp(), movie, camera(thmb(picture))));
    const slice = vi.spyOn(source, 'slice');
    const [result] = await collect(source);
    expect(result).toMatchObject({width: 160, height: 120});
    expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(picture);
    const mediaStart = ftyp().length + 8, mediaEnd = ftyp().length + movie.length;
    expect(slice.mock.calls.every(([start, end]) => end! <= mediaStart || start! >= mediaEnd)).toBe(true);
    expect(result.readBytes).toBeLessThan(1024);
});

test.each(['clip.nev', 'clip.R3D'])('extracts Nikon NCTH from %s, including extended box sizes', async name => {
    const [result] = await collect(file(concat(ftyp('niko'), nikon(box('NCTH', jpeg(), true))), name));
    expect(result).toMatchObject({width: 160, height: 120});
});

test('removes bounded Canon bit padding without accepting arbitrary trailing bytes', async () => {
    const picture = jpeg();
    const [result] = await collect(file(concat(ftyp(), camera(thmb(concat(picture, new Uint8Array([128, 0])))))));
    expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(picture);
    expect(await collect(file(concat(ftyp(), camera(thmb(concat(picture, new Uint8Array([129, 0])))))))).toEqual([]);
});

test('skips corrupt or oversized thumbnail headers and offers Nikon preview', async () => {
    const source = file(concat(ftyp('niko'), nikon(
        box('NCTH', new Uint8Array([255, 216, 255, 217])),
        box('NCTH', jpeg(65535, 65535)), box('NCVW', jpeg(320, 180)),
    )), 'clip.nev');
    expect(await collect(source)).toMatchObject([{width: 320, height: 180}]);
});

test('recognizes the Canon PRVW UUID without scanning for JPEG markers', async () => {
    const prefix = new Uint8Array(16), picture = jpeg(320, 180);
    new DataView(prefix.buffer).setUint32(12, picture.length);
    const source = file(concat(ftyp(), box('uuid', concat(uuidBytes(preview), new Uint8Array(8), box('PRVW', concat(prefix, picture))))));
    expect(await collect(source)).toMatchObject([{width: 320, height: 180}]);
});

test('returns a valid prefix thumbnail before a truncated movie data box', async () => {
    const mdat = box('mdat', new Uint8Array(2));
    new DataView(mdat.buffer).setUint32(0, 1_000_000);
    const source = file(concat(ftyp(), camera(thmb(jpeg())), mdat));
    const iterator = embeddedVideoJpegs(source, new AbortController().signal, 40_000_000);
    expect((await iterator.next()).value).toMatchObject({width: 160, height: 120});
    await iterator.return(undefined);
});

test.each(['clip.mp4', 'clip.braw'])('does not scan unqualified %s files', async name => {
    const source = file(concat(ftyp(), camera(thmb(jpeg()))), name), slice = vi.spyOn(source, 'slice');
    expect(await collect(source)).toEqual([]);
    expect(slice).not.toHaveBeenCalled();
});

test('does not accept unrelated brands, UUIDs or image boxes outside camera contexts', async () => {
    for (const bytes of [concat(ftyp('isom'), camera(thmb(jpeg()))), concat(ftyp(), thmb(jpeg())),
        concat(ftyp(), box('moov', box('uuid', concat(new Uint8Array(16), thmb(jpeg()))))),
        concat(ftyp('niko'), box('NCTH', jpeg()))]) {
        expect(await collect(file(bytes))).toEqual([]);
    }
});

test('rejects out-of-box image references and unsafe extended lengths', async () => {
    const unsafe = box('moov', new Uint8Array(), true);
    new DataView(unsafe.buffer).setBigUint64(8, BigInt('9007199254740993'));
    for (const bytes of [concat(ftyp(), camera(thmb(jpeg(), 10000))), concat(ftyp(), unsafe),
        concat(ftyp(), new Uint8Array([0, 0, 0, 7, 109, 111, 111, 118]))]) {
        expect(await collect(file(bytes))).toEqual([]);
    }
});

test('caps image allocation and total reads even with many image candidates', async () => {
    const picture = new Uint8Array(1500 * 1024);
    picture.set(jpeg().subarray(0, 15)); picture.set([255, 217], picture.length - 2);
    const large = new Uint8Array(EMBEDDED_VIDEO_IMAGE_BYTES + 1);
    const source = file(concat(ftyp('niko'), nikon(box('NCTH', large), ...Array.from({length: 4}, () => box('NCVW', picture)))), 'clip.nev');
    const slice = vi.spyOn(source, 'slice'), rows = await collect(source);
    expect(rows).toHaveLength(2);
    expect(slice.mock.calls.reduce((sum, [start, end]) => sum + end! - start!, 0)).toBeLessThanOrEqual(EMBEDDED_VIDEO_READ_BYTES);
    expect(slice.mock.calls.every(([start, end]) => end! - start! <= EMBEDDED_VIDEO_IMAGE_BYTES)).toBe(true);
});

test('caps traversal without reading unknown UUID bodies', async () => {
    const source = file(concat(ftyp(), ...Array.from({length: 1100}, () => box('uuid', new Uint8Array(16)))));
    const slice = vi.spyOn(source, 'slice');
    expect(await collect(source)).toEqual([]);
    expect(slice.mock.calls.length).toBeLessThanOrEqual(EMBEDDED_VIDEO_MAX_READS);
    expect(slice.mock.calls.every(([start, end]) => end! - start! <= 16)).toBe(true);
});

test('honors cancellation before and after asynchronous slice reads', async () => {
    const source = file(concat(ftyp(), camera(thmb(jpeg()))));
    const controller = new AbortController(); controller.abort();
    const slice = vi.spyOn(source, 'slice');
    await expect(collect(source, controller.signal)).rejects.toThrow('aborted');
    expect(slice).not.toHaveBeenCalled();
    const second = new AbortController();
    slice.mockImplementation(() => ({arrayBuffer: async () => { second.abort(); return new ArrayBuffer(16); }}) as Blob);
    await expect(collect(source, second.signal)).rejects.toThrow('aborted');
});
