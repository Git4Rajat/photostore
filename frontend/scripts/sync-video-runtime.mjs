import {copyFileSync, mkdirSync, readdirSync, rmSync, readFileSync, chmodSync} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const root = path.resolve(import.meta.dirname || path.dirname(new URL(import.meta.url).pathname), '..');
const destination = path.join(root, 'public', 'video-runtime');
mkdirSync(destination, {recursive: true});
for (const obsolete of ['ffmpeg-core.js', 'ffmpeg-core.wasm', 'ffmpeg']) {
    rmSync(path.join(destination, obsolete), {recursive: true, force: true});
}
copyFileSync(require.resolve('mediainfo.js/MediaInfoModule.wasm'), path.join(destination, 'mediainfo-0.3.8.wasm'));
const core = path.resolve(path.dirname(require.resolve('@ffmpeg/core')), '../esm');
for (const name of ['ffmpeg-core.js', 'ffmpeg-core.wasm']) {
    copyFileSync(path.join(core, name), path.join(destination, name.replace('ffmpeg-core', 'ffmpeg-core-0.12.10')));
}
const worker = path.dirname(require.resolve('@ffmpeg/ffmpeg'));
mkdirSync(path.join(destination, 'ffmpeg-0.12.15'), {recursive: true});
for (const name of readdirSync(worker).filter(name => name.endsWith('.js'))) {
    copyFileSync(path.join(worker, name), path.join(destination, 'ffmpeg-0.12.15', name));
}
const licenses = path.join(destination, 'licenses');
mkdirSync(licenses, {recursive: true});
copyFileSync(path.join(path.dirname(require.resolve('mediainfo.js/package.json')), 'LICENSE.txt'), path.join(licenses, 'MediaInfo-BSD-2-Clause.txt'));
copyFileSync(path.join(root, 'video-decoder-source.txt'), path.join(licenses, 'video-decoder-source.txt'));
copyFileSync(path.join(root, 'licenses', 'GPL-2.0.txt'), path.join(licenses, 'FFmpeg-core-GPL-2.0.txt'));
const rawSource = path.join(root, 'vendor', 'prores-raw');
const rawDestination = path.join(destination, 'prores-raw-9.0-r1');
const integrity = JSON.parse(readFileSync(path.join(rawSource, 'integrity.json'), 'utf8'));
mkdirSync(rawDestination, {recursive: true});
for (const [name, digest] of Object.entries(integrity)) {
    if (createHash('sha256').update(readFileSync(path.join(rawSource, name))).digest('hex') !== digest) {
        throw new Error('ProRes RAW runtime integrity mismatch: ' + name);
    }
}
for (const name of readdirSync(rawSource)) {
    copyFileSync(path.join(rawSource, name), path.join(rawDestination, name));
    chmodSync(path.join(rawDestination, name), 0o644);
}
