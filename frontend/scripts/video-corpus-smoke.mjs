import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(repo, 'frontend/package.json'));
const {build} = await import(require.resolve('vite'));
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fixtures = path.resolve(process.argv[2]);
const manifest = JSON.parse(await fs.readFile(path.join(fixtures, 'manifest.json')));
const filters = process.argv.slice(3);
const selected = manifest.rows.filter(row => row.status === 'downloaded' && !row.name.endsWith('.zip')
    && (!filters.length || filters.some(filter => row.name.includes(filter))));
if (!selected.length) throw new Error('No downloaded video samples selected');
const out = path.join(fixtures, 'browser-build'), evidence = path.join(fixtures, 'browser-evidence');
await fs.mkdir(evidence, {recursive: true});
await build({configFile: false, root: repo + '/frontend', publicDir: false,
    build: {outDir: out, emptyOutDir: true, rollupOptions: {input: repo + '/frontend/scripts/video-upload-smoke-entry.ts',
        output: {entryFileNames: 'entry.js'}}}});
await fs.cp(repo + '/frontend/public/video-runtime', path.join(out, 'video-runtime'), {recursive: true});
const receipts = new Map();
const server = http.createServer(async (request, response) => {
    response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; img-src 'self' blob: data:");
    try {
        if (request.url === '/') {
            response.setHeader('Content-Type', 'text/html');
            response.end('<input type="file" multiple><script type="module" src="/entry.js"></script>'); return;
        }
        if (request.url.startsWith('/receipts/')) {
            const parts = request.url.split('/');
            const name = decodeURIComponent(parts[2]), kind = parts[3];
            if (!selected.some(row => row.name === name) || !['metadata', 'thumbnail'].includes(kind)) throw new Error('Invalid receipt');
            const key = name + '.' + kind;
            if (request.method === 'PUT') {
                const chunks = []; let size = 0;
                for await (const chunk of request) {
                    size += chunk.length;
                    if (size > 1024 * 1024) throw new Error('Sidecar budget exceeded');
                    chunks.push(chunk);
                }
                const bytes = Buffer.concat(chunks);
                receipts.set(key, bytes);
                await fs.writeFile(path.join(evidence, key + '.bin'), bytes);
                response.writeHead(201); response.end(); return;
            }
            if (!receipts.has(key)) {response.writeHead(404); response.end(); return;}
            response.setHeader('Content-Type', kind === 'thumbnail' ? 'image/jpeg' : 'application/octet-stream');
            response.end(receipts.get(key)); return;
        }
        const relative = request.url.split('?')[0];
        if (relative.includes('..') || !/^\/[a-zA-Z0-9_./-]+$/.test(relative)) throw new Error('Invalid path');
        const bytes = await fs.readFile(path.join(out, relative));
        response.setHeader('Content-Type', relative.endsWith('.wasm') ? 'application/wasm' : 'text/javascript');
        response.end(bytes);
    } catch {response.writeHead(400); response.end();}
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
    const page = await browser.newPage();
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        return url.hostname === '127.0.0.1' || url.protocol === 'blob:' ? route.continue() : route.abort();
    });
    await page.exposeFunction('saveVideoEvidence', (name, kind, bytes) => fs.writeFile(path.join(evidence, name + '.' + kind), Buffer.from(bytes)));
    await page.exposeFunction('videoProgress', async row => {
        const {metadata, ...brief} = row;
        console.log(JSON.stringify({...brief, metadata: metadata && {bytes: metadata.bytes, readBytes: metadata.readBytes, readCalls: metadata.readCalls}}));
        await fs.writeFile(path.join(evidence, row.name + '.report.json'), JSON.stringify(row, null, 2));
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.locator('input').setInputFiles(selected.map(row => path.join(fixtures, row.name)));
    const rows = await page.evaluate(hashes => window.runVideoSmoke(hashes, true), Object.fromEntries(selected.map(row => [row.name, row.sha256])));
    await fs.writeFile(path.join(evidence, process.env.VIDEO_AUDIT_NAME || 'audit.json'), JSON.stringify({cloudUploads: 0, originalUploads: 0,
        mockedLocalSidecarReceipts: true, browser: await browser.version(), rows}, null, 2));
} finally {await browser?.close(); await new Promise(resolve => server.close(resolve));}
