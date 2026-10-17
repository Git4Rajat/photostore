import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const archive = path.join(root, 'ffmpeg-9.0.tar.xz');
if (createHash('sha256').update(fs.readFileSync(archive)).digest('hex') !== '7f607a00dd0d28a729d5a4811205812eef01cf6ef6155025febb6f36a9062d52') throw new Error('FFmpeg source integrity mismatch');
if (!execFileSync('emcc', ['--version'], {encoding: 'utf8'}).includes('4.0.23')) throw new Error('Emscripten 4.0.23 required');
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'photostore-prores-'));
const run = (command, args, cwd = work) => execFileSync(command, args, {cwd, stdio: 'inherit'});
run('tar', ['-xf', archive]);
const source = path.join(work, 'ffmpeg-9.0');
run('./configure', ['--target-os=none', '--arch=wasm32', '--enable-cross-compile', '--cc=emcc', '--cxx=em++',
    '--ar=emar', '--ranlib=emranlib', '--nm=emnm', '--disable-autodetect', '--disable-everything', '--disable-programs',
    '--disable-network', '--disable-hwaccels', '--disable-doc', '--disable-debug', '--disable-asm', '--disable-x86asm',
    '--disable-inline-asm', '--disable-pthreads', '--disable-w32threads', '--disable-os2threads', '--disable-avdevice',
    '--disable-avfilter', '--disable-swresample', '--enable-decoder=prores_raw', '--enable-demuxer=mov',
    '--enable-protocol=file', '--enable-encoder=mjpeg', '--extra-cflags=-O3 -msimd128', '--extra-ldflags=-O3 -msimd128'], source);
run('make', ['-j', '4'], source);
run('emcc', ['-O3', '-msimd128', '-I' + source, path.join(root, 'thumbnail.c'),
    ...['avformat', 'avcodec', 'swscale', 'avutil'].map(name => path.join(source, 'lib' + name, 'lib' + name + '.a')),
    '-lm', '-lworkerfs.js', '--no-entry', '-sMODULARIZE=1', '-sEXPORT_ES6=1', '-sEXPORT_NAME=ProResRawThumbnail',
    '-sALLOW_MEMORY_GROWTH=1', '-sINITIAL_MEMORY=33554432', '-sMAXIMUM_MEMORY=536870912', '-sSTACK_SIZE=1048576',
    '-sENVIRONMENT=worker', '-sFORCE_FILESYSTEM=1',
    '-sEXPORTED_FUNCTIONS=["_make_prores_thumbnail","_thumbnail_heap_bytes","_malloc","_free"]',
    '-sEXPORTED_RUNTIME_METHODS=["FS","ccall"]', '-o', path.join(root, 'prores-raw.mjs')]);
const manifest = Object.fromEntries(['prores-raw.mjs', 'prores-raw.wasm'].map(name =>
    [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
fs.writeFileSync(path.join(root, 'integrity.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log('Built decoder; intermediate files: ' + work);
