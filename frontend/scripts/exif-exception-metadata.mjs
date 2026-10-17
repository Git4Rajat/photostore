// Compare LibRaw's open-only metadata with ExifTool exceptions; no unpack/develop.
import fs from 'node:fs/promises';
import path from 'node:path';
import {LibRaw} from '@colorhythm/libraw-wasm';

const [root, exceptions, output] = process.argv.slice(2);
if (!root || !exceptions || !output) throw new Error('Usage: node scripts/exif-exception-metadata.mjs CORPUS EXCEPTIONS OUTPUT');
const input = JSON.parse(await fs.readFile(exceptions, 'utf8'));
await LibRaw.initialize();
const rows = [];
for (const item of input.rows) {
    const reader = new LibRaw();
    try {
        await reader.waitUntilReady();
        const bytes = await fs.readFile(path.join(root, item.name));
        reader.open(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
        const camera = reader.getIParams(), metadata = reader.getImgOther();
        const present = value => Number.isFinite(Number(value)) && Number(value) > 0;
        rows.push({name: item.name, opened: true, make: camera.make, model: camera.model,
            captureFields: Object.fromEntries(['iso_speed', 'shutter', 'aperture', 'focal_len', 'timestamp']
                .map(key => [key, present(metadata[key])])),
            hasGpsData: Array.from(metadata.gpsdata).some(value => value !== 0),
            gpsParsed: Boolean(metadata.parsed_gps.gpsparsed),
            cameraNameInBytes: Boolean(camera.model) && bytes.includes(Buffer.from(camera.model)),
            knownContainerHeader: bytes.subarray(0, 2).equals(Buffer.from('II')) || bytes.subarray(0, 2).equals(Buffer.from('MM')),
            sourceBytes: bytes.length});
    } catch (error) {
        rows.push({name: item.name, opened: false, code: typeof error.code === 'number' ? error.code : null});
    } finally {reader.dispose();}
}
const report = {librawVersion: LibRaw.version(), cloudCalls: 0, sensorUnpacks: 0,
    files: rows.length, opened: rows.filter(r => r.opened).length,
    withCaptureFields: rows.filter(r => r.opened && Object.values(r.captureFields).some(Boolean)).length, rows};
await fs.writeFile(output, JSON.stringify(report, null, 2), {mode: 0o600});
console.log(JSON.stringify({...report, rows: undefined}));
