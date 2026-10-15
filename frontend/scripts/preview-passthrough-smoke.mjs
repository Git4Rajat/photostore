import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_PATH || path.resolve(path.dirname(process.execPath), '../node_modules/playwright'));
const base = process.argv[2] || 'http://127.0.0.1:3219';
assert.ok(['127.0.0.1','localhost'].includes(new URL(base).hostname), 'Local preparation only');
const corpus = process.env.IMAGE_DATASET_ROOT || path.join(os.homedir(), 'Documents', 'ImageDataSet');
const output = process.env.PREVIEW_SMOKE_OUTPUT || await fs.mkdtemp(path.join(os.tmpdir(), 'photostore-preview-size-'));
await fs.mkdir(output, {recursive: true});
const browser = await chromium.launch({executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
try {
    const page = await browser.newPage();
    await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== new URL(base).origin) return route.abort();
        if (url.pathname === '/preview-size-smoke.html') return route.fulfill({contentType:'text/html',body:'<!doctype html><input id="source" type="file">'});
        return route.continue();
    });
    const errors = [];
    page.on('pageerror', e=>errors.push(e.message));
    await page.goto(base+'/preview-size-smoke.html');
    await page.evaluate(async () => {
        const {UploadAssetPreparer}=await import('/src/services/uploadAssetPreparation.ts');
        window.preparer = new UploadAssetPreparer();
        window.check = async (file, preserve = true) => {
            const start = performance.now();
            const result = await window.preparer.prepare(file, {preserveEligibleJpeg:preserve});
            if (result.status !== 'generated') throw Error('Preparation failed: '+result.reason);
            const hex = async b=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await b.arrayBuffer())),v=>v.toString(16).padStart(2,'0')).join('');
            const preview = result.preview;
            const originalBitmap=await createImageBitmap(file), bitmap=await createImageBitmap(preview.blob);
            const sample = image=>{
                const c=new OffscreenCanvas(32,32),ctx=c.getContext('2d');ctx.drawImage(image,0,0,32,32);
                return Array.from(ctx.getImageData(0,0,32,32).data);
            };
            const pixels=sample(bitmap),expected=sample(originalBitmap);
            const corners=data=>[8,24].flatMap(y=>[8,24].map(x=>[0,1,2].map(channel=>data[(y*32+x)*4+channel]>=128?'1':'0').join(''))).join(':');
            const row={sourceBytes:file.size,previewBytes:preview.blob.size,thumbnailBytes:result.thumbnail.blob.size,
                width:bitmap.width,height:bitmap.height,sourceWidth:originalBitmap.width,sourceHeight:originalBitmap.height,
                passthrough:preview.passthrough===true,identical:await hex(file)===await hex(preview.blob),
                maximumPixelDelta:Math.max(...pixels.map((v,i)=>Math.abs(v-expected[i]))),
                cornerColorsMatch:corners(pixels)===corners(expected),
                colors:new Set(pixels.filter((_,i)=>i%4!==3)).size,ms:performance.now()-start};
            originalBitmap.close();bitmap.close();return row;
        };
    });
    const fixtures=await page.evaluate(async () => {
        const c=new OffscreenCanvas(80,40),ctx=c.getContext('2d');
        ctx.fillStyle='red';ctx.fillRect(0,0,40,20);ctx.fillStyle='lime';ctx.fillRect(40,0,40,20);
        ctx.fillStyle='blue';ctx.fillRect(0,20,40,20);ctx.fillStyle='yellow';ctx.fillRect(40,20,40,20);
        // Chrome can attach its sRGB ICC profile. Strip metadata only from
        // this known-sRGB synthetic fixture; profile-bearing inputs are tested below.
        const metadataFree=async encoded=>{
            const bytes=new Uint8Array(await encoded.arrayBuffer()),parts=[encoded.slice(0,2)];
            let pos=2;
            while(pos+4<bytes.length){
                const marker=bytes[pos+1];
                if(marker===0xda){parts.push(encoded.slice(pos));break;}
                const end=pos+2+(bytes[pos+2]<<8|bytes[pos+3]);
                if(marker!==0xe1&&marker!==0xe2)parts.push(encoded.slice(pos,end));
                pos=end;
            }
            return new Blob(parts,{type:'image/jpeg'});
        };
        const jpeg=await metadataFree(await c.convertToBlob({type:'image/jpeg',quality:.7}));
        const rows=[{name:'plain',...await window.check(new File([jpeg],'plain.png',{type:'image/png'}))}];
        for(let orientation=1;orientation<=8;orientation++){
            const app1=new Uint8Array([255,225,0,34,69,120,105,102,0,0,77,77,0,42,0,0,0,8,0,1,1,18,0,3,0,0,0,1,0,orientation,0,0,0,0,0,0]);
            const file=new File([jpeg.slice(0,2),app1,jpeg.slice(2)],`orientation-${orientation}.jpg`,{type:'image/jpeg'});
            rows.push({name:'orientation-'+orientation,...await window.check(file)});
        }
        for(const marker of[0xe2,0xee,0xfe]){
            const segment=new Uint8Array([255,marker,0,4,1,1]);
            rows.push({name:'marker-'+marker,...await window.check(new File([jpeg.slice(0,2),segment,jpeg.slice(2)],'metadata.jpg'))});
        }
        const late=new Uint8Array([255,225,0,8,69,120,105,102,0,0]);
        rows.push({name:'late-app1',...await window.check(new File([jpeg.slice(0,-2),late,jpeg.slice(-2)],'late.jpg'))});
        rows.push({name:'trailer',...await window.check(new File([jpeg,'trailer'],'trailer.jpg'))});
        const big=new OffscreenCanvas(3000,1500);big.getContext('2d').drawImage(c,0,0,3000,1500);
        rows.push({name:'oversized-dimensions',...await window.check(new File([await big.convertToBlob({type:'image/jpeg'})],'large.jpg'))});
        const noise=new OffscreenCanvas(1536,1536),noiseCtx=noise.getContext('2d');
        const image=noiseCtx.createImageData(1536,1536);let seed=12345;
        for(let i=0;i<image.data.length;i++){
            seed=(Math.imul(seed,1664525)+1013904223)>>>0;
            image.data[i]=i%4===3?255:seed>>>24;
        }
        noiseCtx.putImageData(image,0,0);
        rows.push({name:'oversized-bytes',...await window.check(new File([await noise.convertToBlob({type:'image/jpeg',quality:1})],'noisy.jpg'))});
        const small=new OffscreenCanvas(384,384);small.getContext('2d').drawImage(noise,0,0);
        rows.push({name:'encoded-smaller',...await window.check(new File([await metadataFree(await small.convertToBlob({type:'image/jpeg',quality:1}))],'small.jpg'))});
        return rows;
    });
    await fs.writeFile(path.join(output,'fixtures.json'),JSON.stringify(fixtures,null,2));
    for(const row of fixtures){
        assert.ok(row.previewBytes<=1_500_000&&Math.max(row.width,row.height)<=2048);
        assert.ok(row.thumbnailBytes<=512_000&&row.colors>1);
        if(row.name==='plain'&&row.passthrough){assert.ok(row.identical);assert.equal(row.maximumPixelDelta,0);}
        else assert.equal(row.passthrough,false,row.name);
        if(row.name.startsWith('orientation-')){
            assert.deepEqual([row.width,row.height],[row.sourceWidth,row.sourceHeight]);
            assert.ok(row.cornerColorsMatch,row.name+' corners must retain native orientation');
        }
        if(row.name==='oversized-bytes')assert.ok(row.sourceBytes>1_500_000);
        if(row.name==='encoded-smaller')assert.ok(row.previewBytes<row.sourceBytes);
    }
    const sampleDir=path.join(corpus,'img_celeba');
    const sources=process.env.PREVIEW_SMOKE_MANIFEST
        ? JSON.parse(await fs.readFile(process.env.PREVIEW_SMOKE_MANIFEST,'utf8'))
        : (await fs.readdir(sampleDir)).filter(n=>/\.jpe?g$/i.test(n)).sort().slice(0,Number(process.env.PREVIEW_SMOKE_FILES||64)).map(n=>({name:n,path:path.join(sampleDir,n)}));
    assert.ok(sources.length);
    const samples=[];
    for(const source of sources){
        const {name}=source;
        assert.ok(path.resolve(source.path).startsWith(path.resolve(corpus)+path.sep));
        await page.locator('#source').setInputFiles(source.path);
        const row=await page.evaluate(async()=>{
            const f=document.querySelector('#source').files[0];
            return {before:await window.check(f,false),after:await window.check(f)};
        });
        assert.ok(row.after.previewBytes<=row.before.previewBytes,name+' must not regress preview bytes');
        if(source.bytes!==undefined)assert.equal(row.after.sourceBytes,source.bytes,name+' must match captured upload size');
        assert.ok(row.after.previewBytes<=1_500_000&&Math.max(row.after.width,row.after.height)<=2048);
        if(row.after.passthrough){
            assert.ok(row.after.identical,name);
            assert.equal(row.after.maximumPixelDelta,0,name);
            assert.equal(row.after.previewBytes,row.after.sourceBytes);
        }
        samples.push({name,...row});
    }
    assert.deepEqual(errors,[]);
    await page.evaluate(()=>window.preparer.dispose());
    const total=key=>samples.reduce((s,r)=>s+r[key].previewBytes,0);
    assert.ok(samples.some(r=>r.after.passthrough),'Dataset must exercise byte reuse');
    const result={status:'passed',browser:browser.version(),fixtures,samples,
        totals:{files:samples.length,reused:samples.filter(r=>r.after.passthrough).length,sourceBytes:samples.reduce((s,r)=>s+r.after.sourceBytes,0),beforePreviewBytes:total('before'),afterPreviewBytes:total('after')},pageErrors:errors};
    await fs.writeFile(path.join(output,'results.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify({status:result.status,browser:result.browser,fixtures:fixtures.length,...result.totals,output,pageErrors:errors}));
}finally{await browser.close();}
