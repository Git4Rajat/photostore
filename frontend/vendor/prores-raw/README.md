# Browser ProRes RAW Thumbnail Decoder

FFmpeg 9.0 (LGPL-2.1-or-later), built with Emscripten 4.0.23. Upstream source:
https://ffmpeg.org/releases/ffmpeg-9.0.tar.xz (included, unmodified).
The separate thumbnail.c adapter and worker use the repository license.
No GPL or nonfree configure flags are enabled in this minimal decoder.
The existing general-purpose FFmpeg core retains its separate GPL notices.

To rebuild or modify the runtime, install/activate Emscripten 4.0.23 with emsdk,
put its emcc/emar/emranlib/emnm on PATH, then run `node build.mjs` from this
directory. This uses the bundled, hash-verified source archive, emits the glue,
WASM and integrity manifest, and prints the temporary build directory.
Use Python 3.10+ on PATH. Bump the `prores-raw-9.0-r1` asset directory in the sync
script and loader whenever its contents change: those URLs are immutable-cached.
Source, adapter, build inputs and license are also served alongside the decoder.
No SDK or intermediate libraries are shipped to browsers. Runtime is lazy-loaded.

Only MOV sample entries aprn/aprh are routed here, after native decode fails.
One real RAW HQ/aprh Atomos clip is qualified; standard/aprn and other cameras
are not yet qualified. This decodes the first frame, not a representative seek.
Active sensor crop is retained. Camera white balance, black/white levels, color
matrix/gain and sRGB display transfer are applied at thumbnail resolution after
demosaic. This is not a color-grading or Apple-certified reference pipeline.
Non-square samples or rotated tracks are rejected until explicitly supported.
Decode/resource/timeout failures retain the original with missing thumbnail;
they never invoke backend video processing.

Limits: 40 million sensor pixels, 32 MiB packet, 64 MiB source reads, 2048 reads,
512 MiB linear memory, 120px JPEG <=512000 bytes. Parent terminates the worker
on cancellation, completion or a 30-second deadline. WORKERFS mounts the File
without copying the entire original into WASM memory.
