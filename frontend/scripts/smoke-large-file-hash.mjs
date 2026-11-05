import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createReadStream} from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';
import {createServer} from 'vite';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve(path.dirname(process.execPath), '../node_modules/playwright'));
const filename = process.argv[2];
assert.ok(filename, 'Pass a local large-file path');
const expected = createHash('sha256');
for await (const chunk of createReadStream(filename)) expected.update(chunk);
const digest = expected.digest('hex');
const server = await createServer({server:{host:'127.0.0.1',port:0},logLevel:'error'});
let browser;
try {
    await server.listen();
    browser = await chromium.launch({executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
    const page = await browser.newPage();
    const base = `http://127.0.0.1:${server.httpServer.address().port}`;
    await page.route('**/large-file-hash-smoke.html', route => route.fulfill({contentType:'text/html',body:'<input type="file">'}));
    await page.goto(base + '/large-file-hash-smoke.html');
    await page.locator('input').setInputFiles(filename);
    const result = await page.evaluate(async () => {
        const {hashLargeFile} = await import('/src/utils/largeFileHash.ts');
        const file = document.querySelector('input').files[0];
        const started = performance.now();
        return {size:file.size,digest:await hashLargeFile(file),ms:Math.round(performance.now()-started)};
    });
    assert.equal(result.digest,digest);
    console.log(JSON.stringify({...result,matchesNodeSHA256:true}));
} finally {
    await browser?.close();
    await server.close();
}
