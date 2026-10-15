// Copy container metadata ranges unchanged; backend ExifTool interprets them.
const TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4};
const POINTER_TAGS = new Set([0x014a, 0x8290, 0x8769, 0x8825, 0xa005]);
const OLYMPUS_POINTERS = new Set([0x2010, 0x2020, 0x2030, 0x2031, 0x2040, 0x2050,
    0x2100, 0x2200, 0x2300, 0x2400, 0x2500, 0x2600, 0x2700, 0x2800, 0x2900, 0x3000]);
const ascii = (bytes, start, length) => String.fromCharCode(...bytes.subarray(start, start + length));

export async function extractRanges(source, maxBytes = 8 * 1024 * 1024) {
    const ranges = [];
    const notices = [];
    let readBytes = 0;
    let readCalls = 0;
    let jpegPassthrough = null;
    async function read(offset, length, retain = true) {
        if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset + length > source.size) {
            const error = new Error('Out-of-bounds metadata reference');
            error.reference = {offset, length};
            throw error;
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
    async function phaseOne(base, length) {
        const header = await read(base, 12);
        const signature = ascii(header, 0, 8);
        const little = signature.startsWith('IIII');
        if (!(little && signature.slice(5) === 'waR') && !signature.startsWith('MMMMRaw')) {
            throw new Error('Unsupported Phase One header');
        }
        const offset = new DataView(header.buffer).getUint32(8, little);
        const countBytes = await read(base + offset, 8);
        const count = new DataView(countBytes.buffer).getUint32(0, little);
        if (count < 2 || count > 300 || offset + 8 + count * 16 > length) throw new Error('Invalid Phase One directory');
        const entries = await read(base + offset + 8, count * 16);
        const view = new DataView(entries.buffer);
        for (let i = 0; i < count; i++) {
            const entry = i * 16, tag = view.getUint32(entry, little);
            const size = view.getUint32(entry + 8, little);
            const pos = view.getUint32(entry + 12, little);
            if (size <= 4) continue;
            if (pos + size > length) throw new Error('Invalid Phase One value');
            if (tag !== 0x010f) await read(base + pos, size);
        }
    }
    async function canonDecisionData(offset) {
        if (!offset) return;
        const header = await read(offset, 8);
        const view = new DataView(header.buffer);
        if (view.getUint32(0) !== 0xffffffff) throw new Error('Invalid Canon decision data');
        const little = [1, 2, 3].includes(view.getUint32(4, true));
        const version = view.getUint32(4, little);
        if (version === 1 || version === 2) {
            const body = await read(offset + 8, 24);
            const count = new DataView(body.buffer).getUint32(20, little);
            if (!count || count >= 20) throw new Error('Invalid Canon decision records');
            await read(offset + 32, count * 32);
        } else if (version === 3) {
            // Two length-prefixed hashes, followed by the length-prefixed records.
            let pos = offset + 8;
            for (let i = 0; i < 3; i++) {
                const word = await read(pos, 4);
                let length = new DataView(word.buffer).getUint32(0, little);
                if (i === 2) {
                    if (length < 4) throw new Error('Invalid Canon decision length');
                    length -= 4;
                }
                if (length > 0x10000) throw new Error('Canon decision data limit');
                await read(pos + 4, length);
                pos += 4 + length;
            }
        } else throw new Error('Unsupported Canon decision version');
    }
    async function tiff(base, canonCR2 = false, vendor = '') {
        const head = await read(base, 8);
        const order = ascii(head, 0, 2);
        if (order !== 'II' && order !== 'MM') throw new Error('Not TIFF');
        const little = order === 'II';
        const h = new DataView(head.buffer);
        if (![42, 85, 0x4f52, 0x5352].includes(h.getUint16(2, little))) throw new Error('Unsupported TIFF magic');
        let canonMake = false;
        const makerNotes = [];
        const retainedMakerNotes = [];
        const badLinks = [];
        let make = '', model = '';
        async function ifd(relative, depth = 0, directoryBase = base, order = little, vendor = '', followNext = true) {
            const base = directoryBase, little = order;
            if (!relative || visited.has(base + relative)) return;
            if (depth > 24 || visited.size > 256) throw new Error('IFD traversal limit');
            const offset = base + relative;
            visited.add(offset);
            const countBytes = await read(offset, 2);
            const count = new DataView(countBytes.buffer).getUint16(0, little);
            if (count > 4096) throw new Error('Excessive IFD entries');
            const bytes = await read(offset + 2, count * 12 + (followNext ? 4 : 0));
            const view = new DataView(bytes.buffer);
            let sr2Offset = 0, sr2Length = 0, jpegOffset = 0, jpegLength = 0;
            for (let i = 0; i < count; i++) {
                const entry = i * 12;
                const tag = view.getUint16(entry, little);
                const type = view.getUint16(entry + 2, little);
                const n = view.getUint32(entry + 4, little);
                // Some Samsung TIFF directories count zero-filled padding entries.
                if (!tag && !type && !n && !view.getUint32(entry + 8, little)) continue;
                if (!TYPE_SIZES[type]) {
                    const error = new Error('Unsupported TIFF value type');
                    error.invalidTiffType = type;
                    error.directoryRange = {offset, length: 2 + bytes.length};
                    throw error;
                }
                const size = TYPE_SIZES[type] * n;
                let value = bytes.subarray(entry + 8, entry + 12);
                if (size > 4) {
                    const pos = base + view.getUint32(entry + 8, little);
                    try {
                        if (tag === 0x927c && size >= 12) {
                            const signature = await read(pos, 12, false);
                            if (ascii(signature, 0, 4) === 'IIII' || ascii(signature, 0, 4) === 'MMMM') {
                                await phaseOne(pos, size);
                                continue;
                            }
                        }
                        if (tag === 0x0127 && h.getUint16(2, order) === 85) {
                            await jpeg(pos, size);
                            continue;
                        }
                        let retained = size;
                        if (tag === 0xc519 && pos + size > source.size) {
                            const signature = await read(pos, Math.min(9, source.size - pos), false);
                            if (ascii(signature, 4, 5) !== '<?xml') throw new Error('Invalid Hasselblad XML');
                            retained = source.size - pos;
                        }
                        value = await read(pos, retained);
                        if (tag === 0x927c) {
                            retainedMakerNotes.push({offset: pos, length: retained});
                            const sig = ascii(value, 0, 12);
                            if (sig.startsWith('Nikon\0\x02')) await tiff(pos + 10, false, 'nikon');
                            else if (sig.startsWith('OLYMPUS\0')) await ifd(12, depth + 1, pos, little, 'olympus', false);
                            else if (sig.startsWith('OM SYSTEM\0')) await ifd(16, depth + 1, pos, little, 'olympus', false);
                            else if (sig.startsWith('OLYMP\0') || sig.startsWith('EPSON\0')) {
                                await ifd(pos + 8 - base, depth + 1, base, little, 'olympus', false);
                            }
                        }
                    }
                    catch (error) { notices.push({tag, error: error.message, reference: error.reference}); continue; }
                }
                if (canonCR2 && tag === 0x010f && type === 2) canonMake = ascii(value, 0, 5) === 'Canon';
                if (canonCR2 && tag === 0x927c && type === 7) makerNotes.push(value);
                if (depth === 0 && type === 2) {
                    if (tag === 0x010f) make = ascii(value, 0, 128).replace(/\0.*$/, '');
                    if (tag === 0x0110) model = ascii(value, 0, 128).replace(/\0.*$/, '');
                }
                if (vendor === 'olympus' && OLYMPUS_POINTERS.has(tag)) {
                    // Old Olympus subdirectories are inline blocks with under-reported
                    // lengths; newer cameras store a single IFD pointer instead.
                    if ((type === 4 || type === 13) && n !== 1) throw new Error('Invalid Olympus directory pointer');
                    await ifd(view.getUint32(entry + 8, little), depth + 1, base, little, 'olympus-subdir', false);
                } else if ((!vendor && POINTER_TAGS.has(tag)) || type === 13
                    || (vendor === 'nikon' && tag === 0x0011)) {
                    if (n > 128) throw new Error('Excessive metadata pointers');
                    const pointers = new DataView(value.buffer, value.byteOffset, value.byteLength);
                    for (let j = 0; j < Math.min(n, 128); j++) {
                        if (j * 4 + 4 > value.length) break;
                        const childVendor = vendor === 'nikon' ? 'nikon-preview' : vendor;
                        try { await ifd(pointers.getUint32(j * 4, little), depth + 1, base, little, childVendor); }
                        catch (error) { notices.push({tag, error: error.message}); }
                    }
                }
                if (tag === 0xc634 && size === 4) {
                    // Sony's DNGPrivateData may point to a separate SR2 IFD.
                    try { await ifd(view.getUint32(entry + 8, little), depth + 1, base, little, vendor); }
                    catch (error) { notices.push({tag, error: error.message}); }
                }
                if (tag === 0x7200 && size === 4) sr2Offset = view.getUint32(entry + 8, little);
                if (tag === 0x7201 && size === 4) sr2Length = view.getUint32(entry + 8, little);
                if (!vendor || vendor === 'nikon-preview') {
                    if (tag === 0x0201 && size === 4) jpegOffset = view.getUint32(entry + 8, little);
                    if (tag === 0x0202 && size === 4) jpegLength = view.getUint32(entry + 8, little);
                }
            }
            if (sr2Offset && sr2Length) await read(base + sr2Offset, sr2Length);
            if (jpegOffset && jpegLength) await jpeg(base + jpegOffset, jpegLength);
            if (followNext) {
                try { await ifd(view.getUint32(count * 12, little), depth + 1, base, little, vendor); }
                catch (error) {
                    // Mamiya ZD links into its opaque MakerNote block. ExifTool
                    // ignores the bad IFD; accept only if we retain the entire block.
                    if (make === 'Mamiya-OP Co.,Ltd.' && model === 'MAMIYA ZD' && error.invalidTiffType > 255) {
                        badLinks.push(error);
                    } else throw error;
                }
            }
        }
        await ifd(h.getUint32(4, little), 0, base, little, vendor);
        for (const error of badLinks) {
            const link = error.directoryRange;
            if (!retainedMakerNotes.some(r => r.offset <= link.offset && r.offset + r.length >= link.offset + link.length)) throw error;
        }
        if (canonMake) for (const bytes of makerNotes) {
            const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
            if (bytes.length < 2) throw new Error('Incomplete Canon maker notes');
            const count = view.getUint16(0, little);
            if (count > 4096 || 2 + count * 12 > bytes.length) throw new Error('Invalid Canon maker notes');
            for (let i = 0; i < count; i++) {
                const entry = 2 + i * 12;
                if (view.getUint16(entry, little) !== 0x83) continue;
                if (view.getUint16(entry + 2, little) !== 4 || view.getUint32(entry + 4, little) !== 1) {
                    throw new Error('Invalid Canon decision pointer');
                }
                await canonDecisionData(view.getUint32(entry + 8, little));
            }
        }
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
    async function bmffMetadataItems(bytes, fileOffset) {
        function boxes(start, end) {
            const found = [];
            let pos = start;
            while (pos < end) {
                if (found.length >= 4096 || pos + 8 > end) throw new Error('Invalid BMFF item box');
                const view = new DataView(bytes.buffer, bytes.byteOffset + pos, end - pos);
                let length = view.getUint32(0), header = 8;
                if (length === 1) {
                    if (pos + 16 > end) throw new Error('Invalid BMFF extended box');
                    length = Number(view.getBigUint64(8));
                    header = 16;
                }
                if (!length) length = end - pos;
                if (!Number.isSafeInteger(length) || length < header || pos + length > end) throw new Error('Invalid BMFF item box');
                found.push({type: ascii(bytes, pos + 4, 4), start: pos + header, end: pos + length});
                pos += length;
            }
            return found;
        }
        function cursor(box) {
            let pos = box.start;
            return {
                number(length) {
                    if (length < 0 || length > 8 || pos + length > box.end) throw new Error('Invalid BMFF item integer');
                    let value = 0;
                    for (let i = 0; i < length; i++) value = value * 256 + bytes[pos++];
                    if (!Number.isSafeInteger(value)) throw new Error('Unsafe BMFF item integer');
                    return value;
                },
                string() {
                    const start = pos;
                    while (pos < box.end && bytes[pos]) pos++;
                    if (pos === box.end) throw new Error('Unterminated BMFF item string');
                    const value = new TextDecoder().decode(bytes.subarray(start, pos));
                    pos++;
                    return value;
                },
                position: () => pos,
            };
        }
        const meta = boxes(0, bytes.length).find(box => box.type === 'meta');
        if (!meta || cursor(meta).number(4) !== 0) throw new Error('Unsupported BMFF meta version');
        const children = boxes(meta.start + 4, meta.end);
        const infos = children.filter(box => box.type === 'iinf');
        const locations = children.filter(box => box.type === 'iloc');
        if (!infos.length && !locations.length) return;
        if (infos.length !== 1 || locations.length !== 1) throw new Error('Missing or duplicate BMFF item tables');
        const info = cursor(infos[0]);
        const infoVersion = info.number(1);
        info.number(3);
        if (infoVersion > 1) throw new Error('Unsupported BMFF item info version');
        const count = info.number(infoVersion ? 4 : 2);
        const entries = boxes(info.position(), infos[0].end);
        if (count > 4096 || entries.length !== count) throw new Error('Invalid BMFF item count');
        const items = new Map();
        for (const entry of entries) {
            if (entry.type !== 'infe') throw new Error('Invalid BMFF item info entry');
            const c = cursor(entry), version = c.number(1);
            c.number(3);
            if (version > 3) throw new Error('Unsupported BMFF item entry version');
            const id = c.number(version === 3 ? 4 : 2), protection = c.number(2);
            const type = version >= 2 ? String.fromCharCode(...Array.from({length: 4}, () => c.number(1))) : 'mime';
            c.string();
            if (type === 'mime') c.string();
            if (items.has(id)) throw new Error('Duplicate BMFF item ID');
            items.set(id, {metadata: ['Exif', 'mime', 'uri ', 'jpeg'].includes(type), protection, type});
        }
        const location = cursor(locations[0]), version = location.number(1);
        location.number(3);
        if (version > 2) throw new Error('Unsupported BMFF item location version');
        const sizes = location.number(2);
        const offsetSize = sizes >> 12, lengthSize = (sizes >> 8) & 15, baseSize = (sizes >> 4) & 15;
        const indexSize = version ? sizes & 15 : 0;
        if ([offsetSize, lengthSize, baseSize, indexSize].some(size => size > 8)) throw new Error('Invalid BMFF item field size');
        const itemCount = location.number(version === 2 ? 4 : 2);
        if (itemCount > 4096) throw new Error('BMFF item limit');
        const seen = new Set();
        const idats = children.filter(box => box.type === 'idat');
        let extentCount = 0;
        // iloc points into mdat/idat; retain metadata extents, never HEVC image items.
        for (let i = 0; i < itemCount; i++) {
            const id = location.number(version === 2 ? 4 : 2);
            if (seen.has(id) || !items.has(id)) throw new Error('Invalid BMFF location ID');
            seen.add(id);
            const method = version ? location.number(2) & 15 : 0;
            const reference = location.number(2), base = location.number(baseSize), count = location.number(2);
            if ((extentCount += count) > 4096) throw new Error('BMFF extent limit');
            const item = items.get(id);
            if (item.metadata && (item.protection || reference || method > 1 || !count)) throw new Error('Unsupported BMFF metadata location');
            if (item.type === 'jpeg' && count !== 1) throw new Error('Unsupported fragmented BMFF JPEG');
            for (let j = 0; j < count; j++) {
                if (version) location.number(indexSize);
                const offset = location.number(offsetSize), length = location.number(lengthSize);
                if (!item.metadata) continue;
                if (!length) throw new Error('Unbounded BMFF metadata extent');
                let start = base + offset;
                if (method === 1) {
                    if (idats.length !== 1 || start + length > idats[0].end - idats[0].start) throw new Error('Invalid BMFF idat extent');
                    start += fileOffset + idats[0].start;
                }
                if (item.type === 'jpeg') await jpeg(start, length);
                else await read(start, length);
            }
        }
        if (location.position() !== locations[0].end || [...items].some(([id, item]) => item.metadata && !seen.has(id))) {
            throw new Error('Incomplete BMFF item locations');
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
        let absenceCandidate = true;
        for (let i = 0; i < 1024 && pos + 4 <= end; i++) {
            const head = await read(pos, 4);
            if (head[0] !== 255) throw new Error('Invalid JPEG marker');
            if (head[1] === 0xd9) return;
            const size = new DataView(head.buffer).getUint16(2);
            if (size < 2 || pos + size + 2 > end) throw new Error('Invalid JPEG segment');
            await read(pos + 4, size - 2);
            if (![0xc0, 0xc2, 0xc4, 0xdb, 0xdd, 0xda, 0xe0].includes(head[1])) absenceCandidate = false;
            if (head[1] === 0xe0) {
                const identifier = await read(pos + 4, Math.min(size - 2, 5), false);
                if (ascii(identifier, 0, 5) !== 'JFIF\0') absenceCandidate = false;
            }
            if (head[1] === 0xda) return absenceCandidate;
            pos += size + 2;
        }
        throw new Error('Incomplete JPEG header');
    }
    async function jpegWithoutExif() {
        // Retain every marker, including between progressive scans, but not
        // entropy-coded pixels. Legacy header-only packets cannot prove absence.
        ranges.length = 0;
        await read(0, 2);
        let pos = 2, scanBytes = new Uint8Array(), scanOffset = 0;
        let safe = true, jfif = false, frame = null, scanned = false;
        async function nextMarker(start) {
            let cursor = start;
            while (cursor + 1 < source.size) {
                if (cursor < scanOffset || cursor + 1 >= scanOffset + scanBytes.length) {
                    scanOffset = cursor;
                    scanBytes = await read(cursor, Math.min(65536, source.size - cursor), false);
                }
                const index = scanBytes.indexOf(255, cursor - scanOffset);
                if (index < 0) { cursor = scanOffset + scanBytes.length; continue; }
                cursor = scanOffset + index;
                if (index + 1 >= scanBytes.length) continue;
                const marker = scanBytes[index + 1];
                if (marker === 255) cursor++;
                else if (marker === 0 || (marker >= 0xd0 && marker <= 0xd7)) cursor += 2;
                else return cursor;
            }
            throw new Error('Incomplete JPEG scan');
        }
        for (let i = 0; i < 1024 && pos + 2 <= source.size; i++) {
            const marker = await read(pos, 2);
            if (marker[0] !== 255) throw new Error('Invalid JPEG marker');
            if (marker[1] === 0xd9) {
                if (pos + 2 !== source.size) throw new Error('Unsupported JPEG trailer');
                return safe && jfif && frame && scanned ? {width: frame.width, height: frame.height} : null;
            }
            const word = await read(pos + 2, 2);
            const size = new DataView(word.buffer).getUint16(0);
            if (size < 2 || pos + size + 2 > source.size) throw new Error('Invalid JPEG segment');
            const body = await read(pos + 4, size - 2);
            // This classification never changes packet ranges. It only allows
            // byte reuse where canvas rotation/profile conversion is unnecessary.
            if (![0xc0, 0xc2, 0xc4, 0xdb, 0xdd, 0xda, 0xe0].includes(marker[1])) safe = false;
            if (marker[1] === 0xe0) {
                if (body.length !== 14 || ascii(body, 0, 5) !== 'JFIF\0'
                    || body[12] !== 0 || body[13] !== 0) safe = false;
                else jfif = true;
            }
            if (marker[1] === 0xc0 || marker[1] === 0xc2) {
                const components = body[5];
                if (frame || body.length !== 6 + 3 * components || body[0] !== 8 || ![1, 3].includes(components)) safe = false;
                const width = (body[3] << 8) | body[4], height = (body[1] << 8) | body[2];
                if (!width || !height) safe = false;
                frame = {width, height};
            }
            if (marker[1] === 0xda) {
                if (!frame || !body[0] || body[0] > 3 || body.length !== 4 + 2 * body[0]) safe = false;
                scanned = true;
            }
            pos += size + 2;
            if (marker[1] === 0xda) pos = await nextMarker(pos);
        }
        throw new Error('Incomplete JPEG structure');
    }
    async function png() {
        await read(0, 8);
        let pos = 8, sawHeader = false, sawData = false, endedData = false;
        for (let i = 0; i < 4096 && pos + 12 <= source.size; i++) {
            const header = await read(pos, 8);
            const length = new DataView(header.buffer).getUint32(0), type = ascii(header, 4, 4);
            if (!/^[A-Za-z]{2}[A-Z][A-Za-z]$/.test(type) || length > 0x7fffffff || pos + length + 12 > source.size) {
                throw new Error('Invalid PNG chunk');
            }
            if (!sawHeader && (type !== 'IHDR' || length !== 13)) throw new Error('Invalid PNG header');
            if (sawHeader && type === 'IHDR') throw new Error('Duplicate PNG header');
            if (/^[A-Z]/.test(type) && !['IHDR', 'PLTE', 'IDAT', 'IEND'].includes(type)) throw new Error('Unsupported PNG critical chunk');
            if (type === 'fdAT') throw new Error('Unsupported PNG animation data');
            if (type === 'IDAT') {
                if (endedData) throw new Error('Nonconsecutive PNG image data');
                sawData = true;
                // Keep structure and CRC at original offsets, not compressed pixels.
                await read(pos + 8 + length, 4);
            } else {
                if (sawData) endedData = true;
                await read(pos + 8, length + 4);
            }
            sawHeader = true;
            pos += length + 12;
            if (type === 'IEND') {
                if (length || !sawData) throw new Error('Invalid PNG end');
                if (pos < source.size) {
                    const footer = await read(source.size - 8, 8, false);
                    if (ascii(footer, 4, 4) !== 'SEFT') throw new Error('Unsupported PNG trailer');
                }
                return;
            }
        }
        throw new Error('Incomplete PNG chunks');
    }
    async function samsungTrailer() {
        if (source.size < 8) return;
        const footer = await read(source.size - 8, 8, false);
        if (ascii(footer, 2, 6) === 'QDIOBS') throw new Error('Unsupported Samsung trailer');
        if (ascii(footer, 4, 4) !== 'SEFT') return;
        const length = new DataView(footer.buffer).getUint32(0, true);
        if (length < 12 || length >= 0x10000 || length + 8 >= source.size) throw new Error('Invalid Samsung trailer length');
        const start = source.size - length - 8;
        const directory = await read(start, length + 8);
        const view = new DataView(directory.buffer);
        if (ascii(directory, 0, 4) !== 'SEFH') throw new Error('Invalid Samsung trailer header');
        const count = view.getUint32(8, true);
        if (count > 4096 || 12 + count * 12 > length) throw new Error('Invalid Samsung trailer count');
        let firstBlock = start, hasMotionPhoto = false;
        for (let i = 0; i < count; i++) {
            const entry = 12 + i * 12;
            const backwards = view.getUint32(entry + 4, true), size = view.getUint32(entry + 8, true);
            if (view.getUint16(entry + 2, true) === 0x0a30) hasMotionPhoto = true;
            if (backwards > start || size > backwards || size < 8) throw new Error('Invalid Samsung trailer reference');
            firstBlock = Math.min(firstBlock, start - backwards);
            const block = await read(start - backwards, size);
            if (new DataView(block.buffer).getUint32(4, true) + 8 > size) throw new Error('Invalid Samsung trailer item');
        }
        if (kind === 'jpeg-header-ranges' && hasMotionPhoto) {
            // ExifTool scans to EOI before interpreting motion-photo trailers.
            // Never invent a terminator or scan/copy the entire JPEG pixel stream.
            if (!count || firstBlock < 2) throw new Error('Invalid Samsung JPEG trailer boundary');
            const eoi = await read(firstBlock - 2, 2);
            if (eoi[0] !== 255 || eoi[1] !== 217) throw new Error('Unsupported Samsung JPEG trailer boundary');
        }
    }
    const head = await read(0, Math.min(source.size, 256), false);
    const view = new DataView(head.buffer);
    let kind;
    const isPng = ascii(head, 0, 8) === '\x89PNG\r\n\x1a\n';
    if (!isPng && head.length) ranges.push({offset: 0, bytes: head});
    if (isPng) {
        kind = 'png-chunk-ranges';
        await png();
    } else if (head[0] === 255 && head[1] === 216) {
        kind = 'jpeg-header-ranges';
        if (await jpeg(0, source.size)) jpegPassthrough = await jpegWithoutExif();
    } else if (ascii(head, 6, 8) === 'HEAPCCDR') {
        kind = 'ciff-directory-ranges';
        const little = ascii(head, 0, 2) === 'II';
        const base = view.getUint32(2, little);
        await ciff(base, source.size - base, little);
    } else if (['II', 'MM'].includes(ascii(head, 0, 2))) {
        kind = 'tiff-ifd-ranges';
        await tiff(0, ascii(head, 8, 4) === 'CR\x02\0');
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
                    if (type === 'meta') await bmffMetadataItems(box, pos);
                }
                catch (error) { notices.push({type, error: error.message}); }
            }
            pos += length;
        }
    } else throw new Error('Unsupported container signature');

    await samsungTrailer();

    if (notices.length) {
        const error = new Error('Incomplete metadata traversal');
        error.notices = notices.slice(0, 32);
        throw error;
    }
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
    return {kind, notices, chunks, payloadBytes, readBytes, readCalls, jpegPassthrough};
}
