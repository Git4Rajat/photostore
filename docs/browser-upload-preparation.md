# Browser Upload Preparation

## Implementation Status

The normal uploader is integrated behind default-off frontend and backend controls. The approved sandbox treatment enables these controls; source support described here does not imply every pending fix is deployed.

- `frontend/src/services/uploadAssetPreparation.ts`: one active worker, at most four queued files, a 30-second active decoding deadline excluding queue wait, abort/disposal handling and explicit fallback results. Invoke only after original dedup/init, without awaiting preparation in an original-transfer lane.
- `frontend/src/utils/uploadDerivatives.ts`: JPEG previews at most 2048 px on the longest edge and 1,500,000 bytes, thumbnails at most 120 px and 512,000 bytes, no upscaling, bounded encoding retries and dimension/byte validation.
- Normal raster preparation picks an already-compliant original JPEG over the
  generated preview only when the original is smaller, and only after
  native decoding and a complete bounded marker walk qualify it as an 8-bit
  grayscale/three-component JFIF JPEG with matching decoded dimensions, no
  EXIF/XMP/ICC/Adobe/comment/unknown markers, and no trailer. This avoids preview
  byte growth and additional JPEG loss for these files without copying private
  camera metadata into previews. Rotated/profile-bearing/uncertain JPEGs and
  non-JPEG inputs keep canvas conversion; that conversion can still be larger
  than a highly compressed source. Thumbnail generation remains unchanged.
  `preserveEligibleJpeg:false` is an explicit local-comparison opt-out, not the
  ordinary upload default. Preview diagnostics carry a boolean `passthrough`
  browser claim; backend byte/pixel/content verification is still authoritative.

Local replay of the exact 2,220 originals from the October15,2026 study,using
Chrome155: old previews125,002,168 bytes versus107,723,809 bytes after smaller-safe-
JPEG selection (13.8% less).2,180 previews were byte-identical originals with
identical decoded pixels;no per-file preview grew relative to the old path.
Seventeen browser fixtures cover all eight EXIF orientations,metadata before/
after scans,profile/comment rejection,trailers,dimension/byte caps and retaining
an encoded JPEG when it is smaller. This is local preview-payload evidence,
not a deployed upload-speed or Azure-invoice comparison. Run
`frontend/scripts/preview-passthrough-smoke.mjs` against a loopback Vite server;
`PREVIEW_SMOKE_MANIFEST` can supply the exact local source manifest.
- Raster generation uses native orientation-aware decoding. Byte-identical JPEG pass-through is opt-in, gated by actual JPEG signature and size/dimensions; metadata/privacy policy must decide whether to enable it.
- RAW generation uses the existing LibRaw WASM sensor-decoding policy, never embedded-preview extraction. Normal preparation passes a 256 MiB source guard and a 120 MP sensor guard. Fresh workers are terminated after every result to release their WASM memory.
- The benchmark imports the same RAW worker and encoder, preventing algorithm drift. Its original sensor settings and default limits are unchanged.
- Backend `IPWORK_REUSE_EXIF=false` is a new default-off experiment. When enabled, geocoding reuses completed stored EXIF/GPS or successful EXIF from the same job. Pending, failed, malformed or invalid coordinates preserve original fallback. Existing preview reuse remains independently controlled by `IPWORK_REUSE_PREVIEW`.

`AppServicesProvider` starts preparation only after the original passes SHA-256 dedup and upload initialization. Original staging/commit never awaits preparation. Background finalization waits for successful asset PUTs and forwards their references, including in backend processing mode. Existing hashing, adaptive transfer lanes, init/finalize batching, block checkpoints and duplicate skipping remain in place.

Upload completion waits for file finalization, not index readiness or view refreshes.
The light index request uses `wait: false` and leaves the server's existing
coalescing enabled. A stalled index request or refresh handler does not hold
upload cleanup, end telemetry, the hash-refresh request or the next queued batch.
Job polling is nudged immediately and again after enqueue responds, so a slow
indexer cold start cannot strand the queued build outside the polling loop.
Existing index-job completion notifications and data refreshes remain separate
from upload completion. This local change needs deployment and a fresh timing
comparison before any wall-clock or Azure-cost improvement can be claimed.

## Integrated Flow

1. Enable `APP_CONFIG_BROWSER_UPLOAD_PREPARATION=true` on the web container and `BROWSER_UPLOAD_PREPARATION=true` on the upload service for an explicitly approved pilot. The browser also requires the backend's `uploadPreparationEnabled` capability; older/disabled backends remain original-only.
2. `UploadPreparationPipeline` admits at most five files, permits at most two derivative/sidecar PUTs, and waits for preparation admission. Each admitted sidecar PUT has a separate 45-second upload/retry budget. Decoding remains one active worker with four queued files and a 30-second active decoding deadline. Metadata extraction runs in bounded per-file workers. Unsupported, aborted, timed-out and failed PUTs omit their unsuccessful assets from finalization.
3. Authenticated init supplies write SAS URLs for the original's physical thumbnail, `preview/{physical}.jpg`, and private `metadata/{physical}.bin`. The binary packet contains a four-byte big-endian manifest length, UTF-8 JSON manifest and concatenated unchanged source ranges. Its maximum size is 8 MiB; the manifest is at most 64 KiB with at most 4096 sorted, nonoverlapping ranges. Source length is capped at 256 MiB for reconstruction. TIFF/CIFF/BMFF/RAF/MRW/X3F, JPEG marker and PNG/WebP chunk transports are bounded; traversal notices, unknown TIFF types and unsupported containers fail extraction.
4. Finalization verifies actual derivative JPEG byte/pixel caps before recording completion. It validates the metadata descriptor against the library row, original physical identity, original SHA-256 and size, then persists a blob-ETag-bound candidate before enqueueing. It does not parse EXIF or run semantic AI for a derivative-only payload. A failed asset/descriptor does not reject an already committed original. Resume regenerates assets using renewed SAS URLs; it does not persist expired URLs or depend on an old in-memory preparation result.
5. With `IPWORK_REUSE_METADATA_PACKET=true`, the EXIF worker conditionally downloads at most the candidate size, verifies its checksum and envelope, reconstructs a temporary sparse file at original offsets, then invokes the existing timeout-bounded ExifTool parser. Unsupported families, malformed/stale packets, unapproved parser warnings/errors, truncated results or filesystem-only tags fall back to the original. The verified DNG warning exception, PNG qualification and structurally verified EXIF-free JPEG path are detailed below. A client no-data flag or a JPEG header alone cannot establish absence. Temporary filesystem timestamps/permissions are discarded. This worker-only result can retain more than 200 tags, while preserving the backend's existing value/serialized EXIF bounds; the sidecar is binary transport, not a promise that every unknown/binary tag is interpreted or displayed.
6. With `IPWORK_REUSE_EXIF=true`, geo consumes the successful EXIF result or validated persisted GPS instead of downloading the original again. With `IPWORK_REUSE_PREVIEW=true`, OCR/faces/vision use an accepted preview even if an EXIF fallback previously downloaded the original. Completed preview/thumbnail steps are filtered by the existing processing lease flow. Missing/corrupt previews retain original fallback.
7. Metadata sidecars are retained with recoverable Trash and removed with failed-upload cleanup or permanent photo purge. Queue producers and direct worker diagnostics can request `use_original=True` (queue field `imageSource=original`) to bypass metadata, GPS and preview reuse for runnable repair/evaluation steps. This is not yet a UI quality-fallback policy or an automatic small-text/small-face detector.

The worker's experimental family allowlist is ARW, CR2, CR3, DNG, ERF, GPR, MOS, MRW, NEF, PEF, RAF, RW2, RWL, 3FR, JPEG, CRW, FFF, IIQ, NRW, ORF, ORI, RAW, SRW, TIFF, STI, X3F, MEF, HEIC, HEIF, HIF, PNG and WebP. Extensions are not universal camera/version guarantees; the corpus qualification and parser checks remain acceptance gates. Init does not mint metadata SAS URLs for other families, avoiding a sidecar upload that cannot yet be reused. Initial activation should use backend processing mode; mixed browser/backend metadata ownership still needs rollout evaluation.

Local follow-up, 2026-10-16: DCR/KDC packet families and narrowly qualified source
warning profiles are added, with structural coverage guards for repaired Samsung
and Kodak metadata subtrees. The fixed 622-file extractable cohort passes exact
packet parity; 42 other corpus sources remain explicit reader exceptions. See
[EXIF hardening study](exif-hardening-2026-10-16.md) for the full denominator,
scenario matrix, browser verification and deployment limitations.

## Deferred GPR Decoding

GoPro GPR (`.gpr`) pixel-decoder support is explicitly deferred to the far-future
backlog as of 2026-10-16. GPR metadata compatibility does not imply thumbnail,
preview or visual-inference support: the five affected study files expose readable
headers but cannot be unpacked by the current browser/backend decoders. Originals
remain uploadable; retain them and report unsupported preparation/processing,
without placeholders or false empty results. This is not a blocker for the
current hardening work. Future SDK-enabled decoder integration needs separate
pixel, color, orientation, memory, timeout and format-regression qualification.
See [Deferred GPR Pixel Decoding](ipworker-architecture.md#deferred-gpr-pixel-decoding-far-future)
for affected filenames and the decoder-build requirements.

## EXIF Fallback Hardening

Videos have a separate browser-only thumbnail/metadata path with no backend
original fallback, independent of the image rollout flag. See
[Browser-Only Video Preparation](browser-video-upload.md) for supported containers,
codec qualifications, bounds and failure semantics.

- ExifTool 13.55's exact `[Minor] Not decoding some large array(s). Ignore minor errors to decode` warning is allowed only for verified DNG profiles. Apple iPhone 12 Pro / DNG 1.4.0.0 and iPhone 12 Pro Max / DNG 1.6.0.0 require the unsuffixed warning. Canon EOS 5D Mark III / DNG 1.4.0.0 requires `[x3]`, `ProfileHueSatMapData1/2` summaries of 8,100 float values each and `ProfileLookTableData` of 13,824 float values. Autel Robotics XB015 / DNG 1.4.0.0 requires `[x2]` and `ProfileHueSatMapData1/2` summaries of 2,250 float values each. All other camera/version/count combinations fall back.
- Extracted original/packet metadata matches exactly under existing normalization for these qualified profiles. Separate read-only tests also verified all 20 expanded Canon/Autel tables against their original values. The warning and summaries are retained; `-m` and `-b` are not enabled in the worker. Expanding tables would not preserve full values in normalized EXIF, which still shortens individual values beyond 512 characters and bounds serialized storage. A second timeout-bounded, family-4 diagnostic pass checks every warning/error, since ordinary JSON hides duplicate warnings. Any additional warning, parser error, storage truncation or failed check preserves fallback. Insta360's invalid `UserComment` encoding is not exempted.
- Samsung SEFT motion-photo JPEGs retain the original end-of-image marker at the validated boundary before their trailer. Omitting it breaks ExifTool's trailer scan. No terminator is synthesized and the main compressed JPEG scan is not copied. The current Samsung packet can still contain a large motion-video trailer block.
- PNG transports retain chunk headers, CRCs and non-IDAT metadata, including metadata after image data. Compressed IDAT pixels are excluded. Parsed PNG header fields qualify screenshots without camera identity tags; filesystem-only output remains insufficient. Malformed/unsupported structures, animation fdAT, unknown critical chunks and metadata budgets retain fallback.
- EXIF-free JPEG candidates retain a complete marker walk through EOI, including markers between progressive scans, without uploading entropy-coded pixels. The browser scans compressed bytes in bounded chunks without decoding them; existing read budgets and worker deadlines still apply. The absence qualification requires a supported 8-bit baseline/progressive JPEG structure, matching parsed dimensions, complete SOS boundaries, terminal EOI and only recognized structural/JFIF segments. Application metadata, comments, trailers, malformed/incomplete structures, old header-only packets and parser warnings cannot establish absence; this is not a general absence guarantee for every image format.
- A separate metadata-bearing JPEG path accepts complete baseline/progressive marker packets containing recognized EXIF/XMP, ICC, Photoshop/IPTC, Ducky, Adobe or comment segments without requiring camera identity tags. All segments must be retained at original offsets, dimensions must match ExifTool and warnings/errors remain rejected. This returns `reused`, not `reused_no_exif`, and never broadens original-JPEG preview passthrough. The additional complete scan applies only when the source fits the range extractor's payload budget (normally just below 8 MiB); larger camera JPEGs or failed complete walks retain the existing header/trailer transport.
- Static WebP uses `webp-chunk-ranges`: RIFF/chunk headers, VP8X, image header prefixes, EXIF/XMP/ICC and padding are retained, not compressed image/alpha pixels. The backend independently validates bounds, dimensions, metadata flags and every retained metadata chunk, even when camera identity tags exist. Animated/unknown chunks, invalid flags, duplicate chunks, truncation, missing metadata ranges and warnings retain fallback. Successful packets return `reused`, not a verified-absence claim. Init remains feature/capability gated.
- A qualified JPEG absence produces `reused_no_exif` in worker logs. The worker retains structural metadata with an internal `verifiedNoExif` result; only the trusted ipworker packet path records EXIF `no_data` with that provenance. Same-job and persisted geo processing skip original downloads and reverse geocoding when GPS is absent. Browser-provided flags cannot opt into this status. A late browser result cannot override verified absence or trigger another original download; forced repairs remain available after resetting the step. Existing saved header-only packets need regeneration to use this path; the change does not retroactively rewrite uploads.
- Local 2026-10-15 verification of the exact range extractor and real backend ExifTool: all 1,055 uploaded CelebA originals qualified as EXIF-free with zero normalized metadata differences. New packet bytes totaled 882,002 versus 59,225,898 original bytes. This is local transport/extraction verification, not proof of deployment, browser upload-time non-regression, or metered Azure savings.
- Local 2026-10-16 replay of all 198 EXIF fallback originals from the later study recovers 140 JPEGs and 18 WebP files with exact normalized ExifTool parity. Their 138,629,101 original bytes can be represented by 632,362 packet bytes. The remaining 40 cases are 16 original parser warnings, 20 unsupported signatures (nine RAW, ten CRW, one MRW), three content/extension mismatches and one unqualified DCR. Isolated Chrome155 confirms all 178 generated packets byte-identical to the local parity replay, with 20 generation fallbacks and zero page errors/cloud calls. These are local payload results, not deployed savings; existing uploads are not repaired retroactively. JPEG header-only packets need regeneration for broader qualification.
- Local 2026-10-14 verification: all 34 downloaded sample packets accepted with exact normalized original parity. The 664-file RAW corpus retains 620 byte-identical packets and 44 generation fallbacks; reuse increases from 568 to 569 without regressions. These are extraction/regression results, not labeled accuracy, measured Azure bills or proof of deployment.
- Local large-array follow-up on 2026-10-14: real ExifTool 13.55 accepts 39/40 corpus DNG packets with exact normalized original parity, up from 32/40 before the Canon/Autel qualification. Only `sample_013.dng` (Insta360 invalid `UserComment` encoding) still falls back. The Fujifilm X100VI HIF regression also retains exact parity for 175 normalized tags. This replay uses local blob-transport fakes, not new cloud uploads or evidence that this policy is deployed.

## Remaining Work

- Establish upload throughput and finalization-tail non-regression on repeated/counterbalanced real uploads, including metadata sidecars and fallback work. The pipeline's extra PUT lanes are bounded but still compete for the user's network.
- Expand production extractor corpus/version qualification, particularly DNG/TIFF variants and currently excluded families. Compare original versus packet values and GPS, including adversarial files and unknown/binary metadata requirements.
- Wire an operator-facing original-only repair option and establish automatic higher-detail OCR/face fallback policy. The internal source override is implemented and tested.
- Verify constrained/mobile browser memory, closed-tab recovery and orphan handling across deployment/runtime policies.
- Measure metered Azure cost per quality-qualified image; do not activate fleet-wide based on local timing alone.

## Acceptance Gates

- Current upload-init/finalization batching, original SHA-256 dedup and resumability regression tests remain green.
- A repeated, counterbalanced real-upload comparison establishes non-regression in end-to-end completion time and original-transfer throughput. Browser decode time alone cannot establish this; the previous corpus-wide browser-derivative upload run was slower.
- Desktop and constrained/mobile-device replay verifies bounded memory, cancellation, decoder failures and closed-tab recovery.
- Corpus original-versus-packet EXIF comparison establishes approved container coverage. The local prototype's scalar-tag parity is not a certificate that all unknown/binary metadata is preserved.
- Text-rich OCR and face-identity evaluation determines higher-detail fallback rules; 2K is the default input, not a universal quality guarantee.
- Real Azure replica lifetime, metered usage, storage operations and detailed costs establish savings per quality-qualified photo. Local CPU-time reductions are not bill savings.
- The normal frontend Docker build now packages LibRaw/WASM copyright/license files and a decoder source notice under `/decoder-licenses/`. Release review must still verify corresponding-source and license obligations; packaging notices alone is not a legal compliance audit.

## Local Verification

Targeted tests cover preparation/encoding, bounded PUT admission/concurrency/deadlines, upload lanes proceeding while preparation is pending, default-off behavior, init capability gating, finalization-before-queue ordering including renamed files, packet checksum/source/range validation, sparse reconstruction, more-than-200-tag preservation, GPS reuse, packet/preview fallback and explicit original-only processing.

`frontend/scripts/smoke-upload-assets.mjs` uses an isolated headless browser and a loopback Vite server. It tests ten RAW extensions and two raster inputs, enforces byte/pixel caps, checks nonblank output pixels and verifies JPEG pass-through by SHA-256. A synthetic Orientation=6 fixture checks that rotation is baked exactly once into both derivatives. It writes generated test outputs to the OS temporary directory and makes no cloud upload/finalization calls. Override `IMAGE_DATASET_ROOT`, `PLAYWRIGHT_MODULE_PATH` and `CHROME_PATH` as needed. Set `UPLOAD_ASSETS_BUNDLE` to replay the production-bundled service with its own temporary loopback asset server.

Build `frontend/scripts/upload-assets-smoke-entry.ts` with `preserveEntrySignatures: strict` to include all three workers and the metadata utility. This replay additionally checks each transported range byte-for-byte against the original, saves packets and mocks all pipeline PUTs locally. `backend/scripts/verify_upload_metadata.py OUTPUT_DIRECTORY CORPUS_DIRECTORY` verifies those saved packets with real ExifTool and compares normalized original tags, excluding temporary filesystem attributes.

Local 2026-10-13 replay: all 12 preview/thumbnail cases and rotation checks passed; 11 metadata packets were generated unchanged and one raster used browser metadata fallback. Backend reuse accepted seven RAW samples with no normalized-tag differences (ARW, CR2, CR3, NEF, RAF, RW2, PEF). DNG used conservative backend fallback; ORF, SRW and a JPEG disguised as `.png` used family gating. These are integration smoke results, not corpus-wide accuracy or throughput claims.

Verification: 225 targeted backend tests and 60 frontend tests passed, along with TypeScript, shell syntax, production worker bundling and diff whitespace checks. Worker diagnostics include packet reuse/fallback counts, actual metadata download bytes and packet latency; total downloaded volume includes metadata as well as original and preview bytes.
