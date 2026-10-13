import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import http from 'node:http';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve(path.dirname(process.execPath), '../node_modules/playwright'));
let base = process.argv[2] || 'http://127.0.0.1:3217';
const corpus = process.env.IMAGE_DATASET_ROOT || path.join(os.homedir(), 'Documents', 'ImageDataSet');
const selected = [
    ...['arw', 'cr2', 'cr3', 'dng', 'nef', 'raf', 'rw2', 'orf', 'pef', 'srw'].map(ext => path.join(corpus, 'RAW_unique_by_format', `sample_001.${ext}`)),
    path.join(corpus, 'hq50k_images', '029856.png'),
    path.join(corpus, 'hq50k_images', '026517.jpg'),
];
const output = await fs.mkdtemp(path.join(os.tmpdir(), 'photostore-upload-assets-'));
let browser, server;
const rows = [];
try {
    let modulePath = '/src/services/uploadAssetPreparation.ts';
    if (process.env.UPLOAD_ASSETS_BUNDLE) {
        const assets = path.join(process.env.UPLOAD_ASSETS_BUNDLE, 'assets');
        const names = await fs.readdir(assets);
        const entry = names.find(name => (name.startsWith('upload-assets-smoke-entry-') || name.startsWith('uploadAssetPreparation-')) && name.endsWith('.js'));
        assert.ok(entry, 'Preparation bundle entry missing');
        modulePath = '/assets/' + entry;
        server = http.createServer(async (req, res) => {
            const name = names.find(name => req.url === '/assets/' + name);
            if (!name) { res.writeHead(404); res.end(); return; }
            try {
                res.setHeader('Content-Type', name.endsWith('.wasm') ? 'application/wasm' : 'text/javascript');
                res.end(await fs.readFile(path.join(assets, name)));
            } catch { res.writeHead(500); res.end(); }
        });
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        base = `http://127.0.0.1:${server.address().port}`;
    }
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH || (process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined), headless: true});
    const page = await browser.newPage();
    await page.route('**/upload-assets-smoke.html', route => route.fulfill({contentType: 'text/html', body: '<!doctype html><input id="source" type="file">'}));
    await page.goto(base + '/upload-assets-smoke.html');
    await page.evaluate(async modulePath => {
        const {UploadAssetPreparer, createUploadMetadata, UploadPreparationPipeline} = await import(modulePath);
        window.preparer = new UploadAssetPreparer();
        window.createMetadata = createUploadMetadata;
        window.Pipeline = UploadPreparationPipeline;
    }, modulePath);
    const rotation = await page.evaluate(async () => {
        const canvas = new OffscreenCanvas(80, 40);
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = 'red'; ctx.fillRect(0, 0, 40, 40);
        ctx.fillStyle = 'blue'; ctx.fillRect(40, 0, 40, 40);
        const jpeg = await canvas.convertToBlob({type: 'image/jpeg', quality: .95});
        // Complete APP1 EXIF block with big-endian TIFF Orientation=6.
        const exif = new Uint8Array([255,225,0,34,69,120,105,102,0,0,77,77,0,42,0,0,0,8,0,1,1,18,0,3,0,0,0,1,0,6,0,0,0,0,0,0]);
        const file = new File([jpeg.slice(0, 2), exif, jpeg.slice(2)], 'orientation-6.jpg', {type: 'image/jpeg'});
        const result = await window.preparer.prepare(file);
        if (result.status !== 'generated') return result;
        const dimensions = {};
        for (const kind of ['preview', 'thumbnail']) {
            const bitmap = await createImageBitmap(result[kind].blob);
            const rotated = new OffscreenCanvas(bitmap.width, bitmap.height);
            const pixels = rotated.getContext('2d');
            pixels.drawImage(bitmap, 0, 0);
            const top = Array.from(pixels.getImageData(20, 20, 1, 1).data);
            const bottom = Array.from(pixels.getImageData(20, 60, 1, 1).data);
            dimensions[kind] = {width: bitmap.width, height: bitmap.height, top, bottom};
            bitmap.close();
        }
        return {status: 'generated', ...dimensions};
    });
    assert.equal(rotation.status, 'generated');
    for (const kind of ['preview', 'thumbnail']) {
        assert.deepEqual([rotation[kind].width, rotation[kind].height], [40, 80]);
        assert.ok(rotation[kind].top[0] > rotation[kind].top[2]);
        assert.ok(rotation[kind].bottom[2] > rotation[kind].bottom[0]);
    }
    console.log(JSON.stringify({fixture: 'orientation-6', ...rotation}));
    for (const source of selected) {
        await page.locator('#source').setInputFiles(source);
        const result = await page.evaluate(async () => {
            const file = document.querySelector('#source').files[0];
            const began = performance.now();
            const result = await window.preparer.prepare(file, {preserveEligibleJpeg: true});
            if (result.status !== 'generated') return {...result, ms: performance.now() - began};
            const images = {};
            for (const kind of ['preview', 'thumbnail']) {
                const image = result[kind];
                const bytes = new Uint8Array(await image.blob.arrayBuffer());
                let data = '';
                for (let i = 0; i < bytes.length; i += 8192) data += String.fromCharCode(...bytes.subarray(i, i + 8192));
                const bitmap = await createImageBitmap(image.blob);
                const canvas = new OffscreenCanvas(32, 32);
                const ctx = canvas.getContext('2d');
                ctx.drawImage(bitmap, 0, 0, 32, 32);
                const pixels = ctx.getImageData(0, 0, 32, 32).data;
                const colors = new Set();
                for (let i = 0; i < pixels.length; i += 4) colors.add(`${pixels[i]},${pixels[i + 1]},${pixels[i + 2]}`);
                images[kind] = {width: bitmap.width, height: bitmap.height, bytes: bytes.length, type: image.blob.type, passthrough: !!image.passthrough, colors: colors.size, base64: btoa(data)};
                bitmap.close();
            }
            let metadata;
            if (window.createMetadata) {
                try {
                    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
                    const sha = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
                    const packet = await window.createMetadata(file, sha);
                    const bytes = new Uint8Array(await packet.blob.arrayBuffer());
                    const length = new DataView(bytes.buffer).getUint32(0);
                    const manifest = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length)));
                    let offset = 4 + length;
                    for (const range of manifest.ranges) {
                        const original = new Uint8Array(await file.slice(range.offset, range.offset + range.length).arrayBuffer());
                        if (!original.every((byte, index) => byte === bytes[offset + index])) throw new Error('Metadata bytes changed');
                        offset += range.length;
                    }
                    if (offset !== bytes.length) throw new Error('Metadata length mismatch');
                    let data = '';
                    for (let i = 0; i < bytes.length; i += 8192) data += String.fromCharCode(...bytes.subarray(i, i + 8192));
                    metadata = {status: 'generated', ...packet.descriptor, rangeCount: manifest.ranges.length, base64: btoa(data)};
                } catch (error) { metadata = {status: 'fallback', error: String(error)}; }
            }
            return {status: result.status, ms: performance.now() - began, metadata, ...images};
        });
        assert.equal(result.status, 'generated', `${path.basename(source)}: ${result.reason}`);
        for (const kind of ['preview', 'thumbnail']) {
            const item = result[kind];
            assert.equal(item.type, 'image/jpeg');
            assert.ok(item.bytes > 0 && item.bytes <= (kind === 'preview' ? 1_500_000 : 512_000));
            assert.ok(Math.max(item.width, item.height) <= (kind === 'preview' ? 2048 : 120));
            assert.ok(item.colors > 1, 'Derivative canvas must not be blank');
            const bytes = Buffer.from(item.base64, 'base64');
            assert.equal(bytes[0], 255);
            assert.equal(bytes[1], 216);
            if (item.passthrough) assert.equal(createHash('sha256').update(bytes).digest('hex'), createHash('sha256').update(await fs.readFile(source)).digest('hex'));
            await fs.writeFile(path.join(output, `${path.basename(source)}-${kind}.jpg`), bytes);
            delete item.base64;
        }
        if (source.endsWith('029856.png')) assert.equal(result.preview.passthrough, true);
        if (result.metadata?.base64) {
            await fs.writeFile(path.join(output, `${path.basename(source)}-metadata.bin`), Buffer.from(result.metadata.base64, 'base64'));
            delete result.metadata.base64;
        }
        rows.push({name: path.basename(source), ...result});
        console.log(JSON.stringify(rows.at(-1)));
    }
    if (process.env.UPLOAD_ASSETS_BUNDLE) {
        await page.locator('#source').setInputFiles(selected[3]);
        const pipeline = await page.evaluate(async () => {
            const file = document.querySelector('#source').files[0];
            const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
            const sha = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
            const puts = [];
            const originalFetch = window.fetch;
            window.fetch = async (url, options) => {
                if (!String(url).startsWith('https://mock-storage.invalid/')) return originalFetch(url, options);
                puts.push({url, bytes: options.body.size, type: options.body.type});
                return new Response('', {status: 201});
            };
            const service = new window.Pipeline();
            try {
                const prepared = await service.start(file, sha, {previewBlobUrl: 'https://mock-storage.invalid/preview',
                    thumbnailBlobUrl: 'https://mock-storage.invalid/thumb', metadataBlobUrl: 'https://mock-storage.invalid/metadata'}, new AbortController().signal);
                return {puts, prepared};
            } finally { service.dispose(); window.fetch = originalFetch; }
        });
        assert.equal(pipeline.puts.length, 3);
        assert.equal(pipeline.prepared.clientProcessing.preview.alreadyUploaded, true);
        assert.equal(pipeline.prepared.clientProcessing.thumbnail.alreadyUploaded, true);
        assert.equal(pipeline.prepared.clientMetadata.version, 1);
        console.log(JSON.stringify({pipeline: 'passed', ...pipeline}));
    }
    await page.evaluate(() => window.preparer.dispose());
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({browser: await browser.version(), rotation, rows}, null, 2));
    console.log(JSON.stringify({status: 'passed', files: rows.length, output}));
} finally {
    await browser?.close();
    if (server) await new Promise(resolve => server.close(resolve));
}
