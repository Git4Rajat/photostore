/* ExifTool 13.55 under WebPerl (WASM) — classic worker, served statically
 * (mirrors the /video-runtime ffmpeg worker pattern; a module worker can't
 * importScripts the Emscripten glue). Boots once per session, loads the
 * ExifTool lib into the WASM FS, then parses files on demand with the SAME
 * flags the backend uses (`-j -G1 -n`) so stored fields match byte-for-byte.
 *
 * Message in : {id, type:'parse', bytes:Uint8Array, name?:string}
 * Message out: {id, ok:true, json:<exiftool -j object>} | {id, ok:false, outcome, error}
 */
/* eslint-disable */
var BASE = (self.location && self.location.pathname.replace(/worker\.js$/, '')) || '/exiftool-wasm/';
var OUT = [], ERR = [];

function mkdirp(p) {
  var parts = p.split('/').filter(Boolean), cur = '';
  for (var i = 0; i < parts.length; i++) { cur += '/' + parts[i]; try { FS.mkdir(cur); } catch (e) {} }
}

var Module = {
  noInitialRun: true,
  locateFile: function (p) { return BASE + p; },      // find emperl.wasm / emperl.data
  print: function (t) { OUT.push(t); },
  printErr: function (t) { ERR.push(t); },
  quit: function () {},                                // perl exit must not kill the worker
};

// Resolves once the interpreter is up AND the ExifTool lib is in the FS.
self.__boot = new Promise(function (resolve, reject) {
  Module.onRuntimeInitialized = function () {
    (async function () {
      try {
        var pack = new Uint8Array(await (await fetch(BASE + 'lib.pack')).arrayBuffer());
        var idxLen = new DataView(pack.buffer).getUint32(0, true);
        var index = JSON.parse(new TextDecoder().decode(pack.subarray(4, 4 + idxLen)));
        var base = 4 + idxLen;
        mkdirp('/lib');
        for (var i = 0; i < index.length; i++) {
          var e = index[i], full = '/lib/' + e.path;
          mkdirp(full.slice(0, full.lastIndexOf('/')));
          FS.writeFile(full, pack.subarray(base + e.off, base + e.off + e.size));
        }
        FS.writeFile('/exiftool', new Uint8Array(await (await fetch(BASE + 'exiftool')).arrayBuffer()));
        try { FS.mkdir('/tmp'); } catch (e) {}
        ENV.PERL5LIB = '/lib'; ENV.HOME = '/tmp';
        resolve();
      } catch (e) { reject(e); }
    })();
  };
});

importScripts(BASE + 'emperl.js');   // defines Module/FS/ENV/callMain and starts the runtime

self.onmessage = async function (ev) {
  var msg = ev.data || {};
  if (msg.type !== 'parse') return;
  try { await self.__boot; }
  catch (e) { self.postMessage({ id: msg.id, ok: false, outcome: 'tool_unavailable', error: String(e) }); return; }
  OUT = []; ERR = [];
  try {
    FS.writeFile('/in', msg.bytes);
    try { callMain(['/exiftool', '-j', '-G1', '-n', '/in']); }
    catch (e) { if (!/exit/i.test(String(e))) ERR.push(String(e)); }
    try { FS.unlink('/in'); } catch (e) {}
    var txt = OUT.join('\n'), json = null;
    try { json = JSON.parse(txt)[0]; } catch (e) {}
    if (json) self.postMessage({ id: msg.id, ok: true, json: json });
    else self.postMessage({ id: msg.id, ok: false, outcome: 'parse_error', error: (ERR[0] || txt.slice(0, 200)) });
  } catch (e) {
    self.postMessage({ id: msg.id, ok: false, outcome: 'parse_error', error: String(e) });
  }
};
