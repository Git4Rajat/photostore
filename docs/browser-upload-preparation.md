# Browser Upload Preparation

## Implementation Status

The normal uploader is now integrated behind default-off frontend and backend controls. No deployed environment has been changed and no cloud uploads were performed for this integration.

- `frontend/src/services/uploadAssetPreparation.ts`: one active worker, at most four queued files, a 30-second deadline including queue wait, abort/disposal handling and explicit fallback results. Invoke only after original dedup/init, without awaiting preparation in an original-transfer lane.
- `frontend/src/utils/uploadDerivatives.ts`: JPEG previews at most 2048 px on the longest edge and 1,500,000 bytes, thumbnails at most 120 px and 512,000 bytes, no upscaling, bounded encoding retries and dimension/byte validation.
- Raster generation uses native orientation-aware decoding. Byte-identical JPEG pass-through is opt-in, gated by actual JPEG signature and size/dimensions; metadata/privacy policy must decide whether to enable it.
- RAW generation uses the existing LibRaw WASM sensor-decoding policy, never embedded-preview extraction. Normal preparation passes a 256 MiB source guard and a 120 MP sensor guard. Fresh workers are terminated after every result to release their WASM memory.
- The benchmark imports the same RAW worker and encoder, preventing algorithm drift. Its original sensor settings and default limits are unchanged.
- Backend `IPWORK_REUSE_EXIF=false` is a new default-off experiment. When enabled, geocoding reuses completed stored EXIF/GPS or successful EXIF from the same job. Pending, failed, malformed or invalid coordinates preserve original fallback. Existing preview reuse remains independently controlled by `IPWORK_REUSE_PREVIEW`.

`AppServicesProvider` starts preparation only after the original passes SHA-256 dedup and upload initialization. Original staging/commit never awaits preparation. Background finalization waits for successful asset PUTs and forwards their references, including in backend processing mode. Existing hashing, adaptive transfer lanes, init/finalize batching, block checkpoints and duplicate skipping remain in place.

## Integrated Flow

1. Enable `APP_CONFIG_BROWSER_UPLOAD_PREPARATION=true` on the web container and `BROWSER_UPLOAD_PREPARATION=true` on the upload service for an explicitly approved pilot. The browser also requires the backend's `uploadPreparationEnabled` capability; older/disabled backends remain original-only.
2. `UploadPreparationPipeline` admits at most five files, permits at most two derivative/sidecar PUTs, and applies a 45-second overall deadline. Decoding remains one active worker with four queued files and a 30-second decode/queue deadline. Metadata extraction runs in bounded per-file workers. Busy, unsupported, aborted, timed-out and failed PUTs omit their unsuccessful assets from finalization.
3. Authenticated init supplies write SAS URLs for the original's physical thumbnail, `preview/{physical}.jpg`, and private `metadata/{physical}.bin`. The binary packet contains a four-byte big-endian manifest length, UTF-8 JSON manifest and concatenated unchanged source ranges. Its maximum size is 4 MiB; the manifest is at most 64 KiB with at most 4096 sorted, nonoverlapping ranges. Source length is capped at 256 MiB for reconstruction. TIFF/CIFF/BMFF/RAF/MRW/X3F and JPEG header transports are bounded; traversal notices, unknown TIFF types and unsupported containers fail extraction.
4. Finalization verifies actual derivative JPEG byte/pixel caps before recording completion. It validates the metadata descriptor against the library row, original physical identity, original SHA-256 and size, then persists a blob-ETag-bound candidate before enqueueing. It does not parse EXIF or run semantic AI for a derivative-only payload. A failed asset/descriptor does not reject an already committed original. Resume regenerates assets using renewed SAS URLs; it does not persist expired URLs or depend on an old in-memory preparation result.
5. With `IPWORK_REUSE_METADATA_PACKET=true`, the EXIF worker conditionally downloads at most the candidate size, verifies its checksum and envelope, reconstructs a temporary sparse file at original offsets, then invokes the existing timeout-bounded ExifTool parser. Unsupported families, malformed/stale packets, parser warnings/errors, truncated results or filesystem-only tags fall back to the original. Packet-only absence is never accepted as confirmed no EXIF. Temporary filesystem timestamps/permissions are discarded. This worker-only result retains more than 200 tags, while preserving the backend's existing value/serialized EXIF bounds; the sidecar is binary transport, not a promise that every unknown/binary tag is interpreted or displayed.
6. With `IPWORK_REUSE_EXIF=true`, geo consumes the successful EXIF result or validated persisted GPS instead of downloading the original again. With `IPWORK_REUSE_PREVIEW=true`, OCR/faces/vision use an accepted preview even if an EXIF fallback previously downloaded the original. Completed preview/thumbnail steps are filtered by the existing processing lease flow. Missing/corrupt previews retain original fallback.
7. Metadata sidecars are retained with recoverable Trash and removed with failed-upload cleanup or permanent photo purge. Queue producers and direct worker diagnostics can request `use_original=True` (queue field `imageSource=original`) to bypass metadata, GPS and preview reuse for runnable repair/evaluation steps. This is not yet a UI quality-fallback policy or an automatic small-text/small-face detector.

The worker's experimental family allowlist is ARW, CR2, CR3, DNG, ERF, GPR, MOS, MRW, NEF, PEF, RAF, RW2, RWL, 3FR and JPEG. Extensions are not universal camera/version guarantees; the corpus qualification and parser checks remain acceptance gates. Init does not mint metadata SAS URLs for other families, avoiding a sidecar upload that cannot yet be reused. Initial activation should use backend processing mode; mixed browser/backend metadata ownership still needs rollout evaluation.

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
