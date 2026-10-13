// Copy container metadata ranges unchanged; backend ExifTool interprets them.
const TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4};
const POINTER_TAGS = new Set([0x014a, 0x8769, 0x8825, 0xa005]);
const ascii = (bytes, start, length) => String.fromCharCode(...bytes.subarray(start, start + length));

export async function extractRanges(source, maxBytes = 4 * 1024 * 1024) {
    const ranges = [];
    const notices = [];
    let readBytes = 0;
    let readCalls = 0;
    async function read(offset, length, retain = true) {
        if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset + length > source.size) {
            throw new Error('Out-of-bounds metadata reference');
        }
        if (length > maxBytes || readBytes + length > maxBytes * 8) throw new Error('Metadata read budget exceeded');
        const bytes = new Uint8Array(await source.slice(offset, offset + length).arrayBuffer());
        if (bytes.length !== length) throw new Error('Short read');
        readBytes += length;
        readCalls++;
        if (retain && length) ranges.push({offset, bytes});
        return bytes;
    }
    const visited = new Set();
    async function tiff(base) {
        const head = await read(base, 8);
        const order = ascii(head, 0, 2);
        if (order !== 'II' && order !== 'MM') throw new Error('Not TIFF');
        const little = order === 'II';
        const h = new DataView(head.buffer);
        if (![42, 85, 0x4f52, 0x5352].includes(h.getUint16(2, little))) throw new Error('Unsupported TIFF magic');
        async function ifd(relative, depth = 0) {
            if (!relative || visited.has(base + relative)) return;
            if (depth > 24 || visited.size > 256) throw new Error('IFD traversal limit');
            const offset = base + relative;
            visited.add(offset);
            const countBytes = await read(offset, 2);
            const count = new DataView(countBytes.buffer).getUint16(0, little);
            if (count > 4096) throw new Error('Excessive IFD entries');
            const bytes = await read(offset + 2, count * 12 + 4);
            const view = new DataView(bytes.buffer);
            let sr2Offset = 0, sr2Length = 0;
            for (let i = 0; i < count; i++) {
                const entry = i * 12;
                const tag = view.getUint16(entry, little);
                const type = view.getUint16(entry + 2, little);
                const n = view.getUint32(entry + 4, little);
                if (!TYPE_SIZES[type]) throw new Error('Unsupported TIFF value type');
                const size = TYPE_SIZES[type] * n;
                let value = bytes.subarray(entry + 8, entry + 12);
                if (size > 4) {
                    const pos = base + view.getUint32(entry + 8, little);
                    try { value = await read(pos, size); }
                    catch (error) { notices.push({tag, error: error.message}); continue; }
                }
                if (POINTER_TAGS.has(tag) || type === 13) {
                    if (n > 128) throw new Error('Excessive metadata pointers');
                    const pointers = new DataView(value.buffer, value.byteOffset, value.byteLength);
                    for (let j = 0; j < Math.min(n, 128); j++) {
                        if (j * 4 + 4 > value.length) break;
                        try { await ifd(pointers.getUint32(j * 4, little), depth + 1); }
                        catch (error) { notices.push({tag, error: error.message}); }
                    }
                }
                if (tag === 0xc634 && size === 4) {
                    // Sony's DNGPrivateData may point to a separate SR2 IFD.
                    try { await ifd(view.getUint32(entry + 8, little), depth + 1); }
                    catch (error) { notices.push({tag, error: error.message}); }
                }
                if (tag === 0x7200 && size === 4) sr2Offset = view.getUint32(entry + 8, little);
                if (tag === 0x7201 && size === 4) sr2Length = view.getUint32(entry + 8, little);
            }
            if (sr2Offset && sr2Length) await read(base + sr2Offset, sr2Length);
            await ifd(view.getUint32(count * 12, little), depth + 1);
        }
        await ifd(h.getUint32(4, little));
    }
    async function ciff(base, length, little, depth = 0) {
        if (depth > 24 || visited.size > 256) throw new Error('CIFF traversal limit');
        const tail = await read(base + length - 4, 4);
        const dir = base + new DataView(tail.buffer).getUint32(0, little);
        if (visited.has(dir)) return;
        visited.add(dir);
        const countBytes = await read(dir, 2);
        const count = new DataView(countBytes.buffer).getUint16(0, little);
        if (count > 4096) throw new Error('CIFF entry limit');
        const entries = await read(dir + 2, count * 10);
        const view = new DataView(entries.buffer);
        for (let i = 0; i < count; i++) {
            const tag = view.getUint16(i * 10, little);
            if (tag & 0x4000) continue;
            const len = view.getUint32(i * 10 + 2, little);
            const off = base + view.getUint32(i * 10 + 6, little);
            const type = (tag >> 8) & 0x38;
            if (type === 0x28 || type === 0x30) await ciff(off, len, little, depth + 1);
            else if (![0x2005, 0x2007, 0x2008].includes(tag & 0x3fff)) {
                try { await read(off, len); }
                catch (error) { notices.push({tag, error: error.message}); }
            }
        }
    }
    async function bmffMetadataSamples(bytes) {
        function boxes(data) {
            const found = [];
            let pos = 0;
            while (pos + 8 <= data.length) {
                const dv = new DataView(data.buffer, data.byteOffset + pos, data.length - pos);
                let size = dv.getUint32(0), header = 8;
                if (size === 1) { size = Number(dv.getBigUint64(8)); header = 16; }
                if (size === 0) size = data.length - pos;
                if (size < header || pos + size > data.length) break;
                found.push({type: ascii(data, pos + 4, 4), payload: data.subarray(pos + header, pos + size)});
                pos += size;
            }
            return found;
        }
        function descend(box, names) {
            if (!names.length) return box;
            const next = boxes(box.payload).find(b => b.type === names[0]);
            return next ? descend(next, names.slice(1)) : null;
        }
        const movie = boxes(bytes).find(b => b.type === 'moov');
        if (!movie) return;
        for (const track of boxes(movie.payload).filter(b => b.type === 'trak')) {
            const table = descend(track, ['mdia', 'minf', 'stbl']);
            if (!table) continue;
            const children = boxes(table.payload);
            const stsd = children.find(b => b.type === 'stsd');
            if (!stsd || ascii(stsd.payload, 12, 4).toUpperCase() !== 'CTMD') continue;
            const stsz = children.find(b => b.type === 'stsz');
            const stsc = children.find(b => b.type === 'stsc');
            const offsets = children.find(b => b.type === 'stco' || b.type === 'co64');
            if (!stsz || !stsc || !offsets) throw new Error('Missing CTMD sample table');
            const sz = new DataView(stsz.payload.buffer, stsz.payload.byteOffset, stsz.payload.length);
            const sc = new DataView(stsc.payload.buffer, stsc.payload.byteOffset, stsc.payload.length);
            const co = new DataView(offsets.payload.buffer, offsets.payload.byteOffset, offsets.payload.length);
            const sampleCount = sz.getUint32(8), chunkCount = co.getUint32(4), ruleCount = sc.getUint32(4);
            if (sampleCount > 4096 || chunkCount > 4096 || ruleCount > 4096) throw new Error('CTMD sample limit');
            let sample = 0, rule = 0;
            for (let chunk = 1; chunk <= chunkCount; chunk++) {
                while (rule + 1 < ruleCount && sc.getUint32(8 + (rule + 1) * 12) <= chunk) rule++;
                const count = sc.getUint32(12 + rule * 12);
                let pos = offsets.type === 'co64' ? Number(co.getBigUint64(8 + (chunk - 1) * 8)) : co.getUint32(8 + (chunk - 1) * 4);
                for (let i = 0; i < count && sample < sampleCount; i++, sample++) {
                    const length = sz.getUint32(4) || sz.getUint32(12 + sample * 4);
                    await read(pos, length);
                    pos += length;
                }
            }
        }
    }
    async function jpeg(offset, length) {
        const end = offset + length;
        const signature = await read(offset, 2);
        if (signature[0] !== 255 || signature[1] !== 216) throw new Error('Not JPEG');
        let pos = offset + 2;
        for (let i = 0; i < 1024 && pos + 4 <= end; i++) {
            const head = await read(pos, 4);
            if (head[0] !== 255) throw new Error('Invalid JPEG marker');
            if (head[1] === 0xd9) return;
            const size = new DataView(head.buffer).getUint16(2);
            if (size < 2 || pos + size + 2 > end) throw new Error('Invalid JPEG segment');
            await read(pos + 4, size - 2);
            if (head[1] === 0xda) return;
            pos += size + 2;
        }
        throw new Error('Incomplete JPEG header');
    }
    const head = await read(0, Math.min(source.size, 256));
    const view = new DataView(head.buffer);
    let kind;
    if (head[0] === 255 && head[1] === 216) {
        kind = 'jpeg-header-ranges';
        await jpeg(0, source.size);
    } else if (ascii(head, 6, 8) === 'HEAPCCDR') {
        kind = 'ciff-directory-ranges';
        const little = ascii(head, 0, 2) === 'II';
        const base = view.getUint32(2, little);
        await ciff(base, source.size - base, little);
    } else if (['II', 'MM'].includes(ascii(head, 0, 2))) {
        kind = 'tiff-ifd-ranges';
        await tiff(0);
    } else if (ascii(head, 0, 16) === 'FUJIFILMCCD-RAW ') {
        kind = 'raf-header-jpeg-metadata-directories';
        await jpeg(view.getUint32(84), view.getUint32(88));
        const jpegStart = view.getUint32(84);
        for (const pair of [72, 92, 120]) {
            if (jpegStart && pair >= jpegStart) continue;
            const offset = view.getUint32(pair);
            const length = view.getUint32(pair + 4);
            if (!offset || !length) continue;
            try { await read(offset, length); }
            catch (error) { notices.push({pair, error: error.message}); }
        }
        const rawBase = view.getUint32(100);
        if (rawBase && rawBase + 8 < source.size) {
            const rawHead = await read(rawBase, 8);
            if (['II', 'MM'].includes(ascii(rawHead, 0, 2))) await tiff(rawBase);
        }
    } else if (ascii(head, 0, 4) === '\0MRM') {
        kind = 'mrw-pre-sensor-header';
        await read(0, view.getUint32(4) + 8);
    } else if (ascii(head, 0, 4) === 'FOVb') {
        kind = 'x3f-directory-nonimage-sections';
        const tail = await read(source.size - 4, 4);
        const dir = new DataView(tail.buffer).getUint32(0, true);
        const dh = await read(dir, 12);
        if (ascii(dh, 0, 4) !== 'SECd') throw new Error('Invalid X3F directory');
        const count = new DataView(dh.buffer).getUint32(8, true);
        if (count > 256) throw new Error('X3F directory limit');
        const entries = await read(dir + 12, count * 12);
        const ev = new DataView(entries.buffer);
        for (let i = 0; i < count; i++) {
            const off = ev.getUint32(i * 12, true);
            const len = ev.getUint32(i * 12 + 4, true);
            const tag = ascii(entries, i * 12 + 8, 4);
            if (tag !== 'IMA2') await read(off, len);
            else {
                const sh = await read(off, Math.min(len, 28));
                if (len > 30) {
                    const sig = await read(off + 28, 2);
                    if (sig[0] === 255 && sig[1] === 216) await jpeg(off + 28, len - 28);
                }
            }
        }
    } else if (ascii(head, 4, 4) === 'ftyp') {
        kind = 'bmff-non-mdat-boxes';
        let pos = 0;
        for (let i = 0; i < 1024 && pos + 8 <= source.size; i++) {
            const bh = await read(pos, Math.min(16, source.size - pos));
            const bv = new DataView(bh.buffer);
            let length = bv.getUint32(0);
            if (length === 1) length = Number(bv.getBigUint64(8));
            if (length === 0) length = source.size - pos;
            if (length < 8 || pos + length > source.size) throw new Error('Invalid BMFF box');
            const type = ascii(bh, 4, 4);
            if (!['mdat', 'free', 'skip'].includes(type)) {
                try {
                    const box = await read(pos, length);
                    if (type === 'moov') await bmffMetadataSamples(box);
                }
                catch (error) { notices.push({type, error: error.message}); }
            }
            pos += length;
        }
    } else throw new Error('Unsupported container signature');

    if (notices.length) throw new Error('Incomplete metadata traversal');
    ranges.sort((a, b) => a.offset - b.offset);
    const merged = [];
    for (const range of ranges) {
        const last = merged.at(-1);
        if (last && range.offset <= last.offset + last.length) last.length = Math.max(last.length, range.offset + range.bytes.length - last.offset);
        else merged.push({offset: range.offset, length: range.bytes.length});
    }
    const payloadBytes = merged.reduce((sum, r) => sum + r.length, 0);
    if (payloadBytes > maxBytes) throw new Error('Metadata payload budget exceeded');
    const chunks = [];
    for (const r of merged) chunks.push({offset: r.offset, bytes: await read(r.offset, r.length, false)});
    return {kind, notices, chunks, payloadBytes, readBytes, readCalls};
}
