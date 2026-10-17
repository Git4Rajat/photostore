# Browser-Only Video Preparation

Video originals retain the normal chunked upload, SHA-256/deduplication and
finalization path. Thumbnail and metadata preparation run alongside transfer,
including backend-AI mode and with the image preparation rollout flag disabled.
Videos are not sent to image vision, OCR, face or backend preview generation.

## Formats and Decoders

The video classifier accepts MP4, MOV, M4V, WebM, MKV, AVI, 3GP, 3G2, MTS,
M2TS, MPG, MPEG, WMV, ASF, FLV, M2V, MXF, OGV, TS and VOB. These are containers
or elementary streams; support for an extension does not guarantee every codec,
camera, damaged file or encrypted variant within it.

For known CRM/NEV/Nikon R3D NE containers, a bounded embedded-JPEG extractor is
attempted first. It follows Canon camera UUID/THMB/PRVW and Nikon NCDT/NCTH/NCVW
boxes, skipping movie sample data by box length. JPEG dimensions are checked
before decode, camera padding is removed, and a small JPEG is generated with the
normal thumbnail encoder. The embedded JPEG's own aspect ratio is retained;
no frame timestamp or unverified video rotation is inferred.

Missing, malformed or undecodable embedded images leave the native/WASM chain
available; cancellation stops it. Native HTML video capture is attempted next. If unavailable, the browser loads
the pinned single-threaded FFmpeg WASM decoder. WORKERFS mounts the original File
for demand reads rather than copying a multi-GB video into an in-memory filesystem.
Only a single frame is resized to a JPEG thumbnail (120px longest edge, at most
512,000 bytes). No 2K video poster or full-video transcode is generated. A seek to
one second retries frame zero for short/nonseekable clips with no output frame.
Native and FFmpeg display-matrix rotation are applied once by their decoders.

BRAW, R3D, CRM and NEV are classified as video too, preventing the older still-image RAW
backend path from silently decoding/downloading cinema clips. Their proprietary
codec decoding is not qualified or guaranteed by the shipped WASM core.
The four tested CRM/NEV/Nikon R3D NE thumbnail cases now succeed using embedded
images, without decoding their RAW frames. Other RAW families remain unsupported.

## Video Metadata

MediaInfo.js 0.3.8/MediaInfoLib runs in a bounded browser worker. It reads requested
File slices and transports the parser's available General/Video/Audio/etc. fields
as a private `video-mediainfo-json` sidecar, not a claim that all camera maker notes
or every ExifTool tag were extracted. Container/camera metadata is often called
"video EXIF", but does not have a universal EXIF schema.

The sidecar is bound to the original SHA-256/size and has its own digest. Backend
finalization HEAD-binds its ETag, then downloads only this small packet to validate
schema, checksum, original physical identity and size. It does not run ExifTool or
FFmpeg on the original video. Metadata includes duration, dimensions, frame rate,
codec, rotation, available capture dates and camera identity. Available ISO6709 or
MediaInfo formatted locations are normalized into GPS latitude/longitude and
passed to the existing reverse-geocoder, without reading the original.

Raw MediaInfo fields remain in the sidecar. Normalized metadata retains existing
512-character value and 60,000-character serialized storage bounds. Lack of a GPS
field means no usable location was extracted, not proof that the original has
none. Date/time strings preserve their supplied timezone; none is invented.

## Limits and Failures

- Source limit: 10 GiB (the backend's configured upload limit may be lower).
- Metadata: at most 1 MiB, 64 tracks, 4096 scalar fields and nesting depth twelve.
- Parser reads: 256 KiB chunks, at most 32 MiB cumulative / 2048 read requests.
- If the read budget ends after usable headers were parsed, finalize that bounded
  result and explicitly mark `readBudgetReached` in the packet and diagnostics.
  Missing/unrecognized headers still fail. Neither case downloads the original.
- Frame guard: 40 million pixels; decoding is serialized with image preparation.
- Embedded JPEG extraction: 2 MiB per image, 4 MiB cumulative reads, 2048 reads,
  1024 boxes and maximum depth eight. There is no full-file JPEG signature scan.
- Existing preparation admission (five files), active deadline (30s), sidecar PUT
  concurrency (two), 45s PUT/retry budget and abort/dispose behavior remain bounded.
- Decoder/WASM/worker assets are pinned, versioned and self-hosted. Metadata runtime
  is about 2.5 MiB; the approximately 31 MiB FFmpeg WASM core loads only if needed.
  Initial download/compile overhead is real, particularly on slow/mobile clients.

Unsupported, corrupt, encrypted, over-budget or failed preparations keep the
uploaded original. Missing/rejected thumbnail and metadata results are recorded
as terminal failures with no automatic backend retry or original download. Image
fallback behavior remains unchanged. Backend force/recovery/queued-preview paths
and info-panel dimension reads are guarded too; old queued video work is acknowledged
without processing. Normal playback or an explicit original download is still allowed.

Video finalization requires a valid client SHA-256. Legacy clients without it
receive an explicit error, not a server download to calculate it. Non-anonymous
cross-owner filename collisions require an anonymous upload reservation. These
are upload integrity/name requirements, distinct from preparation failures.

Per-file diagnostics record generation, upload, backend acceptance, decoder runtime
and metadata read bytes/calls. Embedded thumbnails additionally report
`embedded_video_jpeg` runtime and their own read bytes/calls. The two assets are independent: failure of one does
not discard the other or reject the original. Browser re-preparation of a retained
file needs a future local-file resubmission workflow; backend repair is intentionally
disabled, and normal duplicate skipping remains enabled.

## Verification and Remaining Coverage

Local 2026-10-14 isolated Chrome tests under a strict production-like CSP passed
27 synthetic files across all 20 ordinary extensions. Both metadata and nonblank,
bounded automatic thumbnails passed; real backend packet validation accepted all
27, including Apple-style make/model, timezone-bearing capture date, GPS and a
90-degree display matrix. Forced WASM thumbnails passed 26/27. The AV1 sample
worked natively in Chrome but not through this WASM core; browsers without native
AV1 decoding retain an explicit thumbnail failure. H.264, HEVC, VP8, VP9, MPEG-4,
MPEG-2, H.263, WMV2, FLV1, Theora and ProRes cases were exercised.

These were small synthetic format/codec tests, not real-camera corpus qualification,
mobile/Safari/Firefox coverage, DRM support, long/multi-GB throughput benchmarks,
or measured Azure savings. No live video upload or deployment has occurred yet.
Ship frontend and backend together; image EXIF/preview behavior is unchanged.

The subsequent [downloaded video study](video-format-study-20261014.md) grew to
34 files, separating ordinary clips, RAW samples and controls. The latest full
replay passed all 22 encoded clips and four of nine RAW thumbnail cases. All four
new embedded JPEG sources match the local ExifTool reference; metadata counts
remain 22/22 encoded and 7/9 RAW. Five RAW thumbnail cases remain unsupported.
The study fixed anamorphic display geometry, bounded partial metadata retention
and nested conformance-detail validation, and records remaining sample gaps.

Reproduce browser replay by bundling `frontend/scripts/video-upload-smoke-entry.ts`
with a file input and evidence callbacks; use generated self-hosted runtime assets.
`backend/tests/test_video_upload.py` covers metadata validation and no-original
contracts. FFmpeg core 0.12.10's known ffprobe return-value issue (#817) is allowed
only for fresh valid bounded dimension JSON, not timeout/error codes; successful
frame decoding and valid JPEG output are independently required.

Runtime notices/licenses are shipped at `/video-runtime/licenses/`.
