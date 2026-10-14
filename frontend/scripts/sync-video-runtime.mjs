import {copyFileSync, mkdirSync, readdirSync, rmSync} from 'node:fs';
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
