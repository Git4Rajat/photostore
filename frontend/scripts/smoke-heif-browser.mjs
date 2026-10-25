import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import http from 'node:http';
import {createServer} from 'vite';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve(path.dirname(process.execPath), '../node_modules/playwright'));
const root = process.env.IMAGE_DATASET_ROOT || path.join(os.homedir(), 'Documents/ImageDataSet');
const output = process.argv[2] || await fs.mkdtemp(path.join(os.tmpdir(), 'photostore-heif-browser-'));
await fs.mkdir(output,{recursive:true});
const files=[];
async function walk(directory) {
    for (const entry of await fs.readdir(directory,{withFileTypes:true})) {
        const filename=path.join(directory,entry.name);
        if (entry.isDirectory()) await walk(filename);
        else if (/\.(heic|heif|hif)$/i.test(entry.name)) files.push(filename);
    }
}
await walk(root);
let server,base,modulePath='/src/services/uploadAssetPreparation.ts';
let browser;
const results=[];
try {
    if (process.env.HEIF_ASSETS_BUNDLE) {
        const assets=path.join(process.env.HEIF_ASSETS_BUNDLE,'assets');
        const names=await fs.readdir(assets);
        modulePath='/assets/'+names.find(name=>name.startsWith('upload-assets-smoke-entry-') && name.endsWith('.js'));
        server=http.createServer(async (req,res)=>{
            const name=names.find(name=>req.url==='/assets/'+name);
            if (!name) {res.writeHead(404);res.end();return;}
            try {res.setHeader('Content-Type',name.endsWith('.wasm')?'application/wasm':'text/javascript');res.end(await fs.readFile(path.join(assets,name)));}
            catch {res.writeHead(500);res.end();}
        });
        await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
        base=`http://127.0.0.1:${server.address().port}`;
    } else {
        server=await createServer({server:{host:'127.0.0.1',port:0},logLevel:'error'});
        await server.listen();
        base=`http://127.0.0.1:${server.httpServer.address().port}`;
    }
    browser=await chromium.launch({executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
    const page=await browser.newPage();
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/heif-smoke.html', route => route.fulfill({contentType:'text/html',body:'<input type="file">',
        headers:process.env.HEIF_ASSETS_BUNDLE?{'Content-Security-Policy':"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; img-src 'self' blob: data:"}:{}}));
    await page.goto(base+'/heif-smoke.html');
    await page.route('**/heif-upload/**',route=>route.fulfill({status:201,body:''}));
    for (const filename of files.sort()) {
        await page.locator('input').setInputFiles(filename);
        const result=await page.evaluate(async ({modulePath,fullPreparation,thumbnailOnly}) => {
            const file=document.querySelector('input').files[0];
            let native;
            try {const bitmap=await createImageBitmap(file,{imageOrientation:'from-image'});native={ok:true,width:bitmap.width,height:bitmap.height};bitmap.close();}
            catch(error) {native={ok:false,error:error.message};}
            const {UploadAssetPreparer}=await import(modulePath);
            const preparer=new UploadAssetPreparer();
            const began=performance.now();
            const prepared=await preparer.prepare(file);
            preparer.dispose();
            const summary={name:file.name,size:file.size,native,ms:Math.round(performance.now()-began),status:prepared.status};
            if (fullPreparation) {
                const {UploadPreparationPipeline}=await import('/src/services/uploadPreparationPipeline.ts');
                const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()));
                const sha=Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('');
                const pipeline=new UploadPreparationPipeline();
                const result=await pipeline.start(file,sha,{...(!thumbnailOnly?{previewBlobUrl:'/heif-upload/preview'}:{}),thumbnailBlobUrl:'/heif-upload/thumbnail',
                    metadataBlobUrl:'/heif-upload/metadata',uploadPreparationEnabled:true},new AbortController().signal);
                summary.pipeline={report:result.preparationReport,exif:result.clientProcessingReport,
                    metadataBytes:result.clientMetadata?.bytes};
                pipeline.dispose();
            }
            if (prepared.status!=='generated') return {...summary,reason:prepared.reason,failureReason:prepared.failureReason};
            const preview=prepared.preview;
            const bitmap=await createImageBitmap(preview.blob);
            const canvas=new OffscreenCanvas(32,32);
            const context=canvas.getContext('2d');context.drawImage(bitmap,0,0,32,32);
            const pixels=context.getImageData(0,0,32,32).data;
            const colors=new Set();
            for(let i=0;i<pixels.length;i+=4)colors.add(`${pixels[i]},${pixels[i+1]},${pixels[i+2]}`);
            bitmap.close();
            let base64='';const bytes=new Uint8Array(await preview.blob.arrayBuffer());
            for(let i=0;i<bytes.length;i+=8192)base64+=String.fromCharCode(...bytes.subarray(i,i+8192));
            return {...summary,preview:{width:preview.width,height:preview.height,bytes:bytes.length,colors:colors.size},
                thumbnail:{width:prepared.thumbnail.width,height:prepared.thumbnail.height,bytes:prepared.thumbnail.blob.size},previewBase64:btoa(base64)};
        },{modulePath,fullPreparation:process.env.HEIF_FULL_PREPARATION==='true',thumbnailOnly:process.env.HEIF_THUMBNAIL_ONLY==='true'});
        if (result.previewBase64) {
            const previewPath=path.join(output,`${results.length}-${path.basename(filename)}.jpg`);
            await fs.writeFile(previewPath,Buffer.from(result.previewBase64,'base64'));
            delete result.previewBase64;result.previewPath=previewPath;
        }
        results.push({file:path.relative(root,filename),...result});
        console.log(JSON.stringify(results.at(-1)));
    }
    await fs.writeFile(path.join(output,'results.json'),JSON.stringify({browser:browser.version(),results,errors},null,2));
    console.log(JSON.stringify({output,total:results.length,generated:results.filter(row=>row.status==='generated').length,errors}));
} finally {await browser?.close();await server.close();}
