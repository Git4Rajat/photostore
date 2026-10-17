# Camera and Phone Video Format Study

Local study, 2026-10-14. This is a practical coverage priority list, not a measured
market-share ranking. Containers, compression codecs and colour profiles are
different things. In particular, Canon C-Log is a log transfer profile, not RAW.
Canon Cinema RAW Light is RAW; ordinary ProRes and ProRes RAW are also distinct.
See [Canon's RAW Light explanation](https://downloads.canon.com/nw/camera/products/cinema-eos/c200/docs/canon-eos-c200-new-4k-uhd-hd-white-paper.pdf)
and [Apple's ProRes RAW description](https://www.apple.com/final-cut-pro/docs/Apple_ProRes_RAW.pdf).

## Practical Format List

| Format / codec family | Usual file extensions | Camera / phone context | Downloaded-file coverage in this run |
| --- | --- | --- | --- |
| H.264 / AVC | `.mp4`, `.mov` | Everyday phones, mirrorless, DSLR, action cameras | iPhone H.264; Panasonic 4K High 4:2:2 AVC |
| H.265 / HEVC | `.mp4`, `.mov` | Efficient phone/camera recording; 8/10-bit variants | HEVC MP4, actual iPhone 12 mini, Fujifilm 10-bit and Canon 4:2:2 10-bit originals |
| HDR / HLG / Dolby Vision | Usually `.mp4`, `.mov` | Colour/HDR signaling, not separate containers or RAW | iPhone Dolby Vision 8.4 / HLG; HDR10 ProRes MXF fixture |
| Apple ProRes 422 / 4444 | `.mov`, sometimes `.mxf` | iPhone Pro, cameras, external recorders and editing | 422 HQ, alpha/4444 fixtures; official ARRI ProRes original; ProRes MXF |
| AVCHD / H.264 MPEG-TS | `.mts`, `.m2ts` | Camcorders and older camera recordings | Canon C100 MTS; M2TS flat-black control |
| Sony XAVC S / S-I / HS | `.mp4` | Sony camera AVC or HEVC families | Original FX30 XAVC AVC 4:2:2 10-bit passed; not every XAVC variant |
| Professional MXF families | `.mxf` | XAVC, XF-AVC, MPEG-2, ProRes, RAW and others | MXF wrapper exercised; not blanket qualification of every codec inside MXF |
| Avid DNxHD / DNxHR | `.mov`, `.mxf` | Recorders and professional editing | DNxHD and DNxHR MOV fixtures |
| Motion JPEG | `.avi`, `.mov` | Older digital cameras and some specialist modes | Actual Canon Digital IXUS AVI |
| 3GPP / 3GPP2 | `.3gp`, `.3g2` | Legacy phones | Sony K750i 3GP and mobile 3G2 sample |
| WebM VP8 / VP9 / AV1 | `.webm` | Browser capture, shared/exported media; not a dominant camera original format | Downloaded VP8 clips; earlier synthetic VP9/AV1 tests are separate |
| Other legacy/export containers | `.mkv`, `.m4v`, `.mpg`, `.mpeg`, `.wmv`, `.asf`, `.flv`, `.ogv`, `.ts`, `.vob` | Compatibility, broadcast, editing and exported libraries | Earlier synthetic tests only; not called camera-original qualification here |

Primary references: [Apple phone recording formats](https://support.apple.com/en-us/109041),
[Android media formats](https://developer.android.com/media/platform/supported-formats),
[Sony XAVC wrappers and codecs](https://pro.sony/s3/2024/06/26113904/Sony_XAVCH_WhitePaper_v1.0.pdf),
[Fujifilm movie settings](https://app.fujifilm-dsc.com/en/manual/x-t5/x-t5_manual_en_s_f.pdf).
These establish supported formats, not their market shares.

### True RAW Video

| RAW family | Files | Local browser metadata | Local browser thumbnail |
| --- | --- | --- | --- |
| Blackmagic RAW | `.braw` | Passed on intact official 1.18 GB sample | Unsupported by shipped decoders |
| RED REDCODE | `.r3d` | Both downloaded test extracts unrecognized | Both unsupported; no valid decoded frame |
| Nikon R3D NE | `.r3d` | Passed on intact 782 MB Nikon ZR original, separately from old R3D extracts | Passed using embedded JPEG; RAW decoding remains unsupported |
| Canon Cinema RAW Light | `.crm` | Passed on intact 129 MB C50 original and publisher-truncated C200 sample | Both passed using embedded JPEG; RAW decoding remains unsupported |
| Nikon N-RAW | `.nev` | Passed on intact 1.26 GB Nikon ZR original | Passed using embedded JPEG; RAW decoding remains unsupported |
| Apple ProRes RAW / RAW HQ | `.mov` | Passed on intact 1.04 GB sample | Unsupported by shipped decoders |
| ARRIRAW | `.mxf`, or `.ari` / `.arx` frame sequences | Passed on intact official 1.69 GB MXF | Unsupported by shipped decoders |
| CinemaDNG | A sequence of `.dng` frames plus optional audio | No sequence-aware video ingestion test | Not implemented as a grouped video clip |

`.ari`, `.arx` and CinemaDNG sequences need explicit sequence ingestion design.
A normal single-photo DNG must not be relabeled as video based only on extension.
The no-backend-fallback video policy is qualified for the classified video files,
not an unimplemented sequence ingestion workflow.

Sources: [Blackmagic RAW samples and desktop SDK platforms](https://www.blackmagicdesign.com/event/blackmagicrawinstaller),
[Nikon video file types](https://download.nikonimglib.com/archive6/YHvK7000LU1C06ZyhA16461cwR37/Z6IIIRG_%28En%2904.pdf),
[ARRI workflow samples](https://www.arri.com/en/learn/camera-systems/camera-sample-footage-reference-image),
[Nikon R3D NE sample catalog](https://www.nikon.com.hk/en/cinema-r3d-sample-downloads).
No vendor desktop SDK was installed or misrepresented as a browser decoder.

### Log Profiles

| Profile family | Local evidence |
| --- | --- |
| Canon Log / Log 2 / Log 3 | Original C50 Log2/Log3 passed metadata/thumbnail; first-generation C-Log remains unqualified |
| Sony S-Log2 / S-Log3 | Original FX30 S-Log3 passed metadata/thumbnail; S-Log2 remains unqualified |
| Panasonic V-Log / V-Log L | Owner-provided S1 V-Log MOV passed thumbnail and bounded metadata extraction |
| Fujifilm F-Log / F-Log2 | Original X-H2S F-Log/F-Log2 passed metadata/thumbnail; [official profile description](https://www.fujifilm-x.com/en-gb/learning-centre/f-log-what-is-it-and-should-you-use-it/) |
| Nikon N-Log | Reviewer-hosted N-RAW/N-Log passed metadata; RAW thumbnail unsupported |
| ARRI Log C / LogC4 | Official ARRI ProRes workflow clip tested; exact curve variants are not inferred from an absent metadata label |
| Apple Log / Log 2 | User-supplied iPhone 16 Pro Max ProRes clip passed metadata/thumbnail checks; Log provenance is user-provided, not an independently verified transfer-curve label. Log2 remains unqualified |

Log profiles are applied to encoded video or to RAW development workflows. A
decoder can produce a valid but flat-looking thumbnail without a log-to-Rec.709
transform. This study does not qualify colour grading, LUT application or HDR
display fidelity. [Canon's sample profile comparison](https://canon.jp/biz/product/camera/pro-imaging/cinema-eos/canon-log/sample)
and [Panasonic sample owner's download page](https://www.dpreview.com/videos/1339966853/dpreview-tv-panasonic-s1-v-log-firmware-update/)
are the profile provenance references.

## First Run Results

25 downloaded clips were replayed with isolated Chrome 155.0.8059.40 on this Mac.
The original source bytes were preserved and SHA-256 checked. Files include
camera originals, codec fixtures, publisher test extracts and negative controls;
these are identified separately in the manifest. The six RAW samples represent
five RAW families because RED has two samples. They do not represent six full
camera-family qualifications.

| Group | Files | Metadata accepted by real backend parser | Thumbnail accepted locally | Partial metadata | Preparation P50 / P95 |
| --- | ---: | ---: | ---: | ---: | --- |
| Ordinary / encoded clips | 16 | 16 | 16 nonblank | 1 | 129 / 478 ms |
| RAW test samples | 6 | 4 | 0 | 0 | 124 / 173 ms |
| Controls: encrypted plus two flat-black clips | 3 | 3 | 2 intentionally black; encrypted failed | 0 | 97 / 280 ms |

Timings include preparation and mocked localhost sidecar PUT receipts. They
exclude hashing, original transfer, cloud finalization and the additional forced
WASM replay. This is one sequential local run, not a sustained mobile throughput
benchmark or measured Azure bill saving. RAW failures are fast failures, not
successful RAW decode timings. The browser cache and runtime compile state
affect these measurements.

Backend verification checked packet/source identity, hashes, schema, size bounds,
JPEG decoding, dimensions, rotation, nonblank positive samples and retained
warnings. It exercised the real metadata parser, not deployed finalization or
Azure Blob ETag binding. Backend no-original/force/recovery behavior is covered
by unit tests for MP4, CRM, NEV, BRAW, R3D and MXF. No originals or derivatives
were uploaded to Azure in this study, and no deployment was performed.

Selected actual measurements:

- BRAW: 1,182,224,992-byte original; 2,536,032 browser read bytes; 7,995-byte metadata packet.
- ProRes RAW: 1,041,311,744-byte original; 7,082,496 browser read bytes; 7,878-byte packet.
- ARRIRAW MXF: 1,688,805,376-byte original; 8,130,560 browser read bytes; 12,533-byte packet.
- Panasonic V-Log: 369,911,039-byte original; 33,301,903 browser read bytes; 10,409-byte explicitly partial packet.
- iPhone 12 mini: make/model and 90-degree rotation verified; portrait thumbnail is 68 x 120.
- Anamorphic MXF: display geometry corrected from 120 x 98 to 120 x 68.

Metadata acceptance is not all-EXIF completeness or OCR/face/vision accuracy.
MediaInfo only exposes its supported fields. GPS absence in its packet is not
proof that no GPS exists anywhere in the original. The DNxHD fixture has conflicting
container/bitstream aspect signaling; the decoder's container interpretation is
retained rather than overriding it with a guessed camera aspect ratio.

## Additional Originals

Eight more unchanged originals were downloaded from public links and replayed
separately with the same isolated browser and local sidecar receipt checks. That
follow-up brought the corpus to 33 video files plus the Blackmagic ZIP. The first
run's audit/results are preserved; follow-up evidence is in
`browser-evidence/audit-extra.json` and `results-extra.json`.

| Original | Actual stream / RAW family | Original bytes | Metadata bytes | Thumbnail |
| --- | --- | ---: | ---: | --- |
| `CC50_-_film2.CRM` | Canon C50 CRAW, 2524 x 1332/25 | 128,780,724 | 12,797 | Unsupported |
| `SFX30_-_film3.MP4` | Sony FX30 XAVC AVC 4:2:2 10-bit, 4K/25 | 134,493,314 | 10,089 | 120 x 68, nonblank |
| `FXH2S_-_film4.MOV` | Fujifilm X-H2S HEVC 4:2:0 10-bit, 1080/50 | 200,120,832 | 7,703 | 120 x 68, nonblank |
| `CC50_-_film6.MP4` | Canon C50 HEVC 4:2:2 10-bit, 4K/25 | 141,450,142 | 8,443 | 120 x 68, nonblank |
| `FXH2S_-_film6.MOV` | Fujifilm X-H2S HEVC 4:2:0 10-bit, 6240 x 4160/25 | 172,499,456 | 7,436 | 120 x 80, nonblank |
| `NZr_-_film7.R3D` | Nikon ZR NR3D, stored 3996 x 2248/25 | 781,914,624 | 4,723 | Unsupported |
| `CC50_-_film10.MP4` | Canon C50 HEVC 4:2:0 10-bit, 4K/25 | 179,314,292 | 8,466 | 120 x 68, nonblank |
| `NZr_-_film1.NEV` | Nikon ZR NRAW, stored 6060 x 3410/25 | 1,258,259,456 | 4,734 | Unsupported |

All eight packets were accepted by the real backend parser. All five encoded
clips passed native and forced-WASM thumbnail checks. All three RAW thumbnails
failed explicitly as `video_decode_unsupported`; no original-download fallback
was used. No packet hit the metadata read cap in this batch.

Preparation P50/P95: encoded clips **193/701 ms**; RAW attempts **104/271 ms**.
The same timing exclusions as the first run apply. Originals total 2,996,832,840
bytes; received metadata plus five thumbnails total 75,801 bytes. This is a
sidecar-size observation, not an Azure upload benchmark or cost-saving claim:
originals still must be uploaded and metadata is incomplete.

Sample provenance is the capture owner's published catalog:
[Canon C50 Log2/Log3 and RAW](https://www.optyczne.pl/113.5-Inne_testy-Canon_EOS_C50_-_test_kamery_Podsumowanie_i_filmy_przyk%C5%82adowe.html),
[Sony FX30 S-Log3](https://www.optyczne.pl/64.5-Inne_testy-Sony_FX30_-_test_trybu_filmowego_Podsumowanie_i_filmy_przyk%C5%82adowe.html),
[Fujifilm X-H2S F-Log/F-Log2](https://www.optyczne.pl/63.5-Inne_testy-Fujifilm_X-H2S_-_test_trybu_filmowego_Podsumowanie_i_filmy_przyk%C5%82adowe.html),
[Nikon ZR N-RAW/R3D NE](https://www.optyczne.pl/107.5-Inne_testy-Nikon_ZR_-_test_trybu_filmowego_Podsumowanie_i_filmy_przyk%C5%82adowe.html).
Profile names are owner-provided provenance, not proof inferred from generic
colour metadata. Some catalog codec/rate descriptions disagree with the files;
the table records actual parsed streams. Parsed Nikon RAW dimensions are larger
than the owner's advertised dimensions; no active-image crop is inferred.

### Camera Metadata Gap

An optional local ExifTool 13.55 reference pass records camera fields in
`results-extra.json`. It reads already-local originals for this audit only,
not in backend upload processing. ExifTool finds make/model on the Canon,
Fujifilm and Nikon originals that MediaInfo does not expose in this run. Sony's
packet preserves a camera-attributes string in real-time metadata, but does not
produce stable Make/Model keys. Accepted packets are useful media metadata,
**not complete EXIF replacements**.

Fujifilm camera capture times differ from MediaInfo's container creation
timestamps. Nikon's raw camera dates omit an offset while container dates are
reported in UTC; they must not be compared as identical wall-clock strings.
No camera values were guessed from filenames and no dates were silently
rewritten. A bounded camera-metadata extractor is still needed for full EXIF
coverage without backend original reads.

### User-Supplied iPhone Clip

`Free ProRes Log Stock Footage 21.MOV` was downloaded unchanged from the
[user-supplied Drive file](https://drive.google.com/file/d/1z65XLwkskjH-4w2dTxN4Y4jC62CucC79/view?pli=1).
It is 2,381,066,480 bytes; its SHA-256 was recorded during download and rechecked
independently from the completed file. Camera metadata identifies Apple iPhone
16 Pro Max, 3840 x 2160 ProRes 422 HQ, approximately 60 fps, duration 10.87 s.
The original file was downloaded, not Drive's streaming proxy. Apple Log is the
user-provided profile provenance; neither parser exposes an explicit transfer
curve label here, and BT.2020 primaries alone do not prove that curve.

Browser preparation passed in 836 ms, excluding hashing and original transfer:
13,859-byte metadata packet, 1,802-byte nonblank 120 x 68 JPEG. The thumbnail used
the in-browser FFmpeg WASM path, not backend fallback. MediaInfo read 5,780,328
bytes in 23 reads, below its caps. The backend accepted the actual received
metadata packet, and make/model matched the local ExifTool reference.

This file exposed an altitude suffix in MediaInfo's `Recorded_Location` that
previously prevented GPS normalization. The parser now accepts an optional
signed metres suffix while retaining coordinate bounds and rejecting malformed
suffixes. Normalized latitude/longitude match the local ExifTool reference.
Tests cover altitude, both hemispheres, boundary/invalid values, sidecar-only
reads and handing coordinates to the existing geocoding path. No real geocoding
or Azure processing was invoked. Other camera-EXIF gaps remain unresolved.

The corpus now contains 34 videos: 22 encoded clips, nine RAW samples and three
controls. This one-file replay is preserved separately in
`browser-evidence/audit-apple-log.json` and `results-apple-log.json`.

## Embedded Thumbnail Hardening

The latest full 34-file replay is in `browser-evidence/audit-embedded-thumbnails.json`
and `results-embedded-thumbnails.json`. Historical earlier runs remain separately
recorded; their RAW thumbnail failures describe the implementation at that time.

| Targeted sample | Thumbnail source read bytes / calls | Generation time | Uploaded JPEG bytes | Dimensions |
| --- | ---: | ---: | ---: | --- |
| Canon C200 CRM, publisher-truncated test extract | 10,768 / 16 | 6 ms | 2,322 | 120 x 90 |
| Canon C50 CRM, intact | 7,403 / 16 | 5 ms | 1,779 | 120 x 90 |
| Nikon ZR N-RAW NEV | 8,695 / 12 | 5 ms | 2,164 | 120 x 90 |
| Nikon ZR R3D NE | 8,204 / 12 | 5 ms | 1,768 | 120 x 90 |

All four produced nonblank JPEGs using the new `embedded_video_jpeg` path.
The browser-extracted source bytes match ExifTool's `ThumbnailImage` bytes after
removing container padding. All are 160 x 120 source JPEGs; their aspect ratio
and letterboxing are preserved, rather than being stretched to video dimensions.
Canon's THMB zero/bit padding is accepted within a bounded tail; arbitrary
trailing bytes, malformed references and oversized JPEG dimensions are rejected.
The larger embedded preview is available as an alternative if the small image
cannot be decoded. Neither native video nor FFmpeg is needed for the four
successful production preparation paths; the test harness's additional forced
WASM RAW attempts still fail, as expected.

The extractor follows only recognized Canon and Nikon box structures. It does
not scan `mdat` for JPEG markers or copy whole videos into memory. Limits:
2 MiB per candidate image, 4 MiB cumulative reads, 2048 reads, 1024 boxes, depth
eight, and the existing 40-million-pixel frame guard. Malformed/missing images
leave existing browser decoding available; cancellation propagates and bitmaps
are closed. Upload concurrency, output caps, metadata extraction and backend
no-original/no-fallback rules are unchanged.

Full replay results: encoded thumbnails **22/22**, RAW thumbnails **4/9**;
metadata remains **22/22 encoded** and **7/9 RAW**, with Panasonic still marked
partial. Encrypted and flat-black controls behave as before. Encoded preparation
P50/P95 is **171/749 ms**; RAW preparation P50/P95 is **102/182 ms**, mixing
successful embedded extraction and fast failures. These are sequential local
measurements with the earlier timing exclusions, not cloud upload/cost results.

```bash
VIDEO_AUDIT_NAME=audit-embedded-thumbnails.json PLAYWRIGHT_MODULE=/Users/rajat.verma1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs \
  node frontend/scripts/video-corpus-smoke.mjs /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014
TZ=UTC .venv-photostore-tools/bin/python backend/verify_video_corpus.py /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014 \
  --audit audit-embedded-thumbnails.json --output results-embedded-thumbnails.json --exiftool /private/tmp/photostore-installed-exiftool-bin/exiftool
```

## Fixes Made

1. Added CRM and NEV to both frontend/backend video classifiers and file-picker
   accept lists. They retain the original and never use still-image backend fallback.
2. Corrected WASM thumbnail scaling for non-square pixels using decoder display
   aspect ratio and a square-pixel JPEG result. Rotation regression replay passed.
3. Finalize bounded MediaInfo analysis with an empty chunk at the read limit,
   preserving usable headers rather than throwing away everything. Packet and
   diagnostics mark `readBudgetReached` / `metadata_read_budget`; the backend stores
   `MetadataReadBudgetReached`. The 32 MiB / 2048-read caps did not increase.
4. Distinguished unknown/invalid frame dimensions from genuinely oversized frames
   in failure diagnostics. The 40-million-pixel cap stays intact.
5. Allowed depth 12 for genuine nested MediaInfo conformance details, retaining
   the 1 MiB packet and 4096-field caps and adding rejection tests above the limit.
6. Removed obsolete unversioned generated decoder assets from runtime sync, avoiding
   a second unused copy of the approximately 31 MiB FFmpeg core in deployment artifacts.
7. Accept MediaInfo recorded-location altitude suffixes so browser-supplied iPhone
   GPS reaches the stable coordinate keys without backend original reads.
8. Extract and re-encode bounded embedded video JPEGs before loading browser video
   decoders, with source-byte verification, cancellation and hostile-container tests.

Current verification: 377 frontend tests pass; typecheck and production build
pass; 103 targeted backend tests pass. The full backend suite was not requalified by
this run; previously identified unrelated baseline failures remain separate.

## Artifacts and Reproduction

Local corpus: `/Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014`.
`manifest.json` records URLs, provenance, categories, hashes and sizes;
`results.json` records every result and normalized tags. `browser-evidence/`
holds per-file browser claims, actual localhost sidecar bytes, packets and JPEGs.
Keep footage private: public download availability is not a redistribution license.
The [FFmpeg sample collection](https://samples.ffmpeg.org/00-README) explicitly
contains damaged and obscure fixtures, so a downloaded file is not automatically
a valid camera recording. No generated/re-encoded clip was labeled camera RAW.

```bash
cd /Users/rajat.verma1/photostore/frontend
npm run sync:video-runtime
cd /Users/rajat.verma1/photostore
PLAYWRIGHT_MODULE=/Users/rajat.verma1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs \
  node frontend/scripts/video-corpus-smoke.mjs /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014
.venv-photostore-tools/bin/python backend/verify_video_corpus.py /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014
```

To reproduce only the follow-up while preserving the first run:

```bash
VIDEO_AUDIT_NAME=audit-extra.json PLAYWRIGHT_MODULE=/Users/rajat.verma1/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs \
  node frontend/scripts/video-corpus-smoke.mjs /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014 CC50_ SFX30_ FXH2S_ NZr_
TZ=UTC .venv-photostore-tools/bin/python backend/verify_video_corpus.py /Users/rajat.verma1/Documents/ImageDataSet/video_samples_20261014 \
  --audit audit-extra.json --output results-extra.json --exiftool /private/tmp/photostore-installed-exiftool-bin/exiftool
```

## Remaining Work

Independently qualify Apple Log transfer-curve identity, obtain/test Log2,
first-generation C-Log, S-Log2, additional
XAVC/XF-AVC variants and mobile/Safari/Firefox behavior. The
expired publicly shared Nikon owner links were checked and returned 404. No
registration, marketing consent, purchase or authentication gate was bypassed.
The older 4.96 GB candidate from a
[public grading discussion](https://www.reddit.com/r/cinematography/comments/16p78va/apple_log_grading/)
was not downloaded; the supplied 2.38 GB sample was preferred. Do not substitute
HDR/graded footage and call its transfer curve Apple Log without provenance.

Remaining RAW clips without usable embedded images require additional compatible browser decoders and a
licensing/resource-budget review. The current setup does not provide those decoders.
Until then, preserve RAW originals and any extracted metadata, explicitly record
missing thumbnails, and keep backend original-download/decode fallback disabled.
Sequence-aware CinemaDNG/ARI/ARX support is a separate implementation requirement.
