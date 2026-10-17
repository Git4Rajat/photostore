/* ExifTool 13.55 under WebPerl (WASM) — classic worker, served statically
 * (mirrors the /video-runtime ffmpeg worker pattern; a module worker can't
 * importScripts the Emscripten glue). Boots once per session, loads the
 * ExifTool lib into the WASM FS, then parses files on demand with the SAME
 * flags the backend uses (`-j -G1 -n`) so stored fields match byte-for-byte.
 *
 * Message in : {id, type:'parse', bytes:Uint8Array, name?:string} (name keeps the extension ExifTool uses to pick TIFF-based RAW types)
 * Message out: {id, ok:true, json:<exiftool -j object>} | {id, ok:false, outcome, error}
 */
/* eslint-disable */
// Nikon maker-note tags whose in-WASM values disagree with native ExifTool 13.55 (verified on the
// 664-file RAW corpus). Dropped from every browser parse; the rest of the Nikon output is kept.
const NIKON_EXCLUDED_TAGS = new Set([
  'Nikon:AutoDistortionControl', 'Nikon:AutoISO', 'Nikon:BracketIncrement', 'Nikon:BracketProgram',
  'Nikon:BracketSet', 'Nikon:DiffractionCompensation', 'Nikon:FlashMasterControlMode',
  'Nikon:FlickerReductionShooting', 'Nikon:FocusShiftExposureLock', 'Nikon:FocusShiftInterval',
  'Nikon:FocusShiftNumberShots', 'Nikon:FocusShiftStepWidth', 'Nikon:HDR', 'Nikon:HDRLevel',
  'Nikon:HighFrameRate', 'Nikon:ImageArea', 'Nikon:IntervalDurationHours', 'Nikon:IntervalDurationMinutes',
  'Nikon:IntervalDurationSeconds', 'Nikon:IntervalExposureSmoothing', 'Nikon:IntervalFrame',
  'Nikon:IntervalPriority', 'Nikon:IntervalShooting', 'Nikon:Intervals', 'Nikon:MovieType',
  'Nikon:MultipleExposureMode', 'Nikon:NikonMeteringMode', 'Nikon:PitchAngle', 'Nikon:PixelShiftDelay',
  'Nikon:PixelShiftInterval', 'Nikon:PixelShiftNumberShots', 'Nikon:PixelShiftShooting',
  'Nikon:PortraitImpressionBalance', 'Nikon:ReleaseMode', 'Nikon:RollAngle', 'Nikon:SecondarySlotFunction',
  'Nikon:ShotsPerInterval', 'Nikon:SingleFrame', 'Nikon:SubjectDetection', 'Nikon:SubjectDetectionAreaMF',
  'Nikon:TimeZone', 'Nikon:VRMode', 'Nikon:YawAngle', 'NikonCustom:AF-AssistIlluminator',
  'NikonCustom:AF-CPrioritySelection', 'NikonCustom:AF-SPrioritySelection', 'NikonCustom:AFActivation',
  'NikonCustom:AFOnButton', 'NikonCustom:AFPointSel', 'NikonCustom:ApertureLock',
  'NikonCustom:AutoBracketModeM', 'NikonCustom:AutoBracketOrder', 'NikonCustom:AutoFlashISOSensitivity',
  'NikonCustom:BlockShotAFResponse', 'NikonCustom:CLModeShootingSpeed', 'NikonCustom:CenterWeightedAreaSize',
  'NikonCustom:CmdDialsReverseRotExposureComp', 'NikonCustom:CmdDialsReverseRotation',
  'NikonCustom:ContinuousModeDisplay', 'NikonCustom:ControlRingResponse',
  'NikonCustom:EasyExposureCompensation', 'NikonCustom:ExposureControlStepSize',
  'NikonCustom:ExtendedShutterSpeeds', 'NikonCustom:FileNumberSequence',
  'NikonCustom:FineTuneOptCenterWeighted', 'NikonCustom:FineTuneOptHighlightWeighted',
  'NikonCustom:FineTuneOptMatrixMetering', 'NikonCustom:FineTuneOptSpotMetering',
  'NikonCustom:FlashExposureCompArea', 'NikonCustom:FlashShutterSpeed', 'NikonCustom:FlashSyncSpeed',
  'NikonCustom:FocusPointPersistence', 'NikonCustom:Func1Button', 'NikonCustom:Func2Button',
  'NikonCustom:HighSpeedSync', 'NikonCustom:ISOStepSize', 'NikonCustom:ImageReviewMonitorOffTime',
  'NikonCustom:LensControlRing', 'NikonCustom:LensFunc1Button', 'NikonCustom:LensFunc2Button',
  'NikonCustom:ManualFocusRingInAFMode', 'NikonCustom:MaxContinuousRelease',
  'NikonCustom:MenuMonitorOffTime', 'NikonCustom:ModelingFlash', 'NikonCustom:MovieAF-OnButton',
  'NikonCustom:MovieAFSpeed', 'NikonCustom:MovieAFSpeedApply', 'NikonCustom:MovieAFTrackingSensitivity',
  'NikonCustom:MovieFunc1Button', 'NikonCustom:MovieFunc2Button', 'NikonCustom:MovieLensControlRing',
  'NikonCustom:MultiSelectorPlaybackMode', 'NikonCustom:MultiSelectorShootMode',
  'NikonCustom:PlaybackMonitorOffTime', 'NikonCustom:SelfTimerShotCount',
  'NikonCustom:SelfTimerShotInterval', 'NikonCustom:SelfTimerTime', 'NikonCustom:ShootingInfoMonitorOffTime',
  'NikonCustom:ShutterReleaseButtonAE-L', 'NikonCustom:ShutterSpeedLock', 'NikonCustom:StoreByOrientation',
]);
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
    var ext = ((msg.name || '').match(/\.[A-Za-z0-9]{1,8}$/) || [''])[0].toLowerCase();
    var inPath = '/in' + ext;
    FS.writeFile(inPath, msg.bytes);
    try { Module.callMain(['/exiftool', '-j', '-G1', '-n', inPath]); }
    catch (e) { if (!/exit/i.test(String(e))) ERR.push(String(e)); }
    try { FS.unlink(inPath); } catch (e) {}
    var txt = OUT.join('\n'), json = null;
    try { json = JSON.parse(txt)[0]; } catch (e) {}
    if (json) Object.keys(json).forEach(function (k) { if (NIKON_EXCLUDED_TAGS.has(k)) delete json[k]; });
    if (json) self.postMessage({ id: msg.id, ok: true, json: json });
    else self.postMessage({ id: msg.id, ok: false, outcome: 'parse_error', error: (ERR[0] || txt.slice(0, 200)) });
  } catch (e) {
    self.postMessage({ id: msg.id, ok: false, outcome: 'parse_error', error: String(e) });
  }
};
