// Exercise the browser range extractor locally without uploading corpus data.
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {extractRanges} from '../src/utils/metadataRanges.js';

const [rootArg, outputArg, pattern] = process.argv.slice(2);
if (!rootArg || !outputArg) throw new Error('Usage: node scripts/exif-corpus-packets.mjs CORPUS OUTPUT [REGEX]');
const root = path.resolve(rootArg), output = path.resolve(outputArg);
if (root === output || root.startsWith(output + path.sep) || output.startsWith(root + path.sep)) {
    throw new Error('Output must be separate from corpus');
}
const filter = pattern ? new RegExp(pattern) : null;
await fs.mkdir(output, {recursive: true, mode: 0o700});
const rows = [];
for (const entry of (await fs.readdir(root, {withFileTypes: true})).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isFile() || entry.name.startsWith('.') || (filter && !filter.test(entry.name))) continue;
    const name = entry.name, sourcePath = path.join(root, name), handle = await fs.open(sourcePath, 'r');
    try {
        const stat = await handle.stat(), digest = createHash('sha256');
        for await (const chunk of createReadStream(sourcePath)) digest.update(chunk);
        const sourceSha256 = digest.digest('hex');
        const source = {size: stat.size, slice: (start, end) => ({arrayBuffer: async () => {
            const bytes = new Uint8Array(end - start);
            const {bytesRead} = await handle.read(bytes, 0, bytes.length, start);
            return bytes.buffer.slice(0, bytesRead);
        }})};
        const row = {name, sourceBytes: stat.size, sourceSha256}, start = performance.now();
        try {
            const result = await extractRanges(source, 8 * 1024 * 1024 - 64 * 1024);
            const manifest = Buffer.from(JSON.stringify({version: 1, kind: result.kind, sourceSize: stat.size,
                sourceSha256, ranges: result.chunks.map(c => ({offset: c.offset, length: c.bytes.length}))}));
            if (manifest.length > 64 * 1024 || result.chunks.length > 4096) throw new Error('Manifest limit');
            const header = Buffer.alloc(4); header.writeUInt32BE(manifest.length);
            const packet = Buffer.concat([header, manifest, ...result.chunks.map(c => Buffer.from(c.bytes))]);
            if (packet.length > 8 * 1024 * 1024) throw new Error('Packet limit');
            await fs.writeFile(path.join(output, name + '.bin'), packet, {mode: 0o600});
            Object.assign(row, {status: 'generated', kind: result.kind, readBytes: result.readBytes,
                readCalls: result.readCalls, packet: {version: 1, bytes: packet.length, sourceSha256,
                    sha256: createHash('sha256').update(packet).digest('hex')}});
        } catch (error) {
            Object.assign(row, {status: 'failed', reason: error.message});
        }
        row.generationMs = performance.now() - start;
        rows.push(row);
    } finally {await handle.close();}
    if (rows.length % 50 === 0) console.log(JSON.stringify({files: rows.length, generated: rows.filter(r => r.status === 'generated').length}));
}
await fs.writeFile(path.join(output, 'index.json'), JSON.stringify({root, rows}, null, 2), {mode: 0o600});
console.log(JSON.stringify({files: rows.length, generated: rows.filter(r => r.status === 'generated').length}));
