# EXIF Hardening, 2026-10-16

Local investigation and fixes. Not committed, published or deployed by this
study. No cloud originals were read, no photos uploaded, and no dataset originals
modified. The earlier original-byte handoff changes remain local as well.

## Acceptance Contract

The primary target is at least 99% **exact metadata-packet reuse** among a fixed
reference cohort with extractable camera metadata, with zero accepted mismatches.
Success includes GPS, capture fields, normalized tags and the complete ExifTool
diagnostic multiset. A warning does not itself mean missing metadata, but a
warning exemption must not silently accept partial metadata.

This is not 99% accuracy on arbitrary future files, and not 99% of every selected
upload. Unsupported/corrupt sources, verified absence, upload admission and packet
reuse are distinct outcomes. Successful model inference and Azure bills are not
measured here.

Reference reader: pinned ExifTool 13.55 with the application's existing
normalization/storage limits (512 characters/value and approximately 60,000
characters/entity). Filesystem dates and permissions describe the reconstructed
file and are excluded. This does not certify unbounded binary/raw-tag retention.

## Corpus Results

Current `RAW_unique_by_format`: 664 files, including two HEICs. This is not the
historical 1,808-file inventory.

| Measure | Before | After |
| --- | ---: | ---: |
| Original-reader qualifying camera metadata | 622 | 622 |
| Generated browser packets | 620 | 622 |
| Accepted packets with exact metadata and diagnostic parity | 576 | 622 |
| Exact reuse / fixed extractable cohort | 92.60% | 100% |
| Accepted mismatches | 0 | 0 |
| Unresolved original-reader exceptions | 42 | 42 |

The fixed-cohort gate passes with no reference-extraction regressions or
infrastructure failures. A smaller index, changed source hashes, incomplete
report, changed runtime, added/missing corpus files or newly failing reference
extractions cannot shrink its denominator. Target enforcement requires both the
baseline index and report; ordinary subset observations cannot pass that gate.

The real isolated Chrome metadata worker processed all 664 files: 622 descriptors
and packet hashes matched the Node reference, with 42 explicit fallbacks. Of the
old packets, 614 remain byte-identical; only five KDC and three SRW cases changed.
Generated metadata-worker duration: median 14.2 ms, p95 46.2 ms, maximum 75.5 ms.
These clocks exclude hashing, preview generation, network transfer and backend
extraction; they do not establish upload-throughput non-regression.

An additional 34 downloaded JPEG, PNG, TIFF, RAW, DNG and HEIF/HIF samples were
checked. Under the stricter complete-container JPEG reader, 32 reuse packets with
exact normalized metadata and diagnostic parity. Two source-structure exceptions
remain fallback: the iPhone 15 Pro sample has stale MPF offsets pointing beyond
the file (ExifTool `-ee` reports an MPImage2 read warning), and the Samsung
panorama lacks the expected JPEG end marker before its SEFT trailer. Do not repair
these indexes or invent terminators merely to pass a test. The PNG has no
qualifying camera identity but its structural metadata is still preserved.

Across both sets this is 653/655 camera-bearing files (99.69%) reusing exact
packets. This pooled file count is not independent camera-model coverage or a
deployed success-rate estimate; some downloaded formats share source provenance.

## Failure Scenarios And Changes

| Scenario | Required Behavior |
| --- | --- |
| Valid metadata packet | Source/hash/ETag/size binding, bounded reconstruction, qualification, reuse |
| Verified metadata-free JPEG packet | EXIF `no_data`; geo without GPS does not fetch an original or geocode |
| Missing/unsupported browser packet | Not evidence of EXIF absence; retain existing photo fallback policy |
| Parser error, timeout, missing tool, invalid output, read failure | Bounded error code and failed status, not `no_data` |
| Explicit unsupported reader diagnostic | `exif_unsupported`, not successful metadata or verified absence |
| Known source warning | Camera/runtime-specific qualification plus exact complete diagnostic multiset |
| Unknown/additional warning or error | Reject packet qualification; preserve the conservative fallback |
| Old incomplete SRW/KDC packet | Reject despite matching camera/warning; require retained metadata subtree coverage |
| Invalid checksum, stale source or missing range | Reject; never treat sparse zero-filled holes as retained metadata |
| Packet/source/storage limit | Explicit exception/fallback, not silent success |
| Valid metadata with no GPS | No original re-read solely to search for GPS; no reverse geocoding |
| Partial/malformed/nonfinite GPS | Geo failure, not verified GPS absence |
| EXIF retry after completed geo | Preserve completed geo; do not downgrade it |
| Same-job extraction error | Do not immediately repeat extraction during geo or result saving |
| Trusted verified-empty result | Carry absence provenance; browser flags cannot bypass verification |

Samsung Type2 MakerNote preview directories can lie just outside the declared
MakerNote. Their directory and JPEG metadata headers are now retained without
scan pixels. Kodak's private metadata directory and legacy CameraInfo references
are now followed; the two malformed Interop references are tolerated only for
the verified camera/layout and retained evidence. Synthetic negative tests cover
pointer corruption, gaps, limits and unknown profiles.

All 54 warning-bearing extractable originals now retain exact parity. Exemptions
are pinned by extension, file type, camera, runtime and exact warnings, with
additional DNG-version checks where applicable. Warnings remain in stored
metadata. No `-m`, broad warning suppression or arbitrary camera acceptance was
introduced. Family-4 diagnostics reveal warnings hidden by ordinary JSON output;
see [ExifTool's duplicate-tag guidance](https://exiftool.org/faq.html).

PNG original fallback uses ExifTool without Pillow pixel decoding or holding the
Pillow metadata lock. PNG now requires the pinned tool, rather than pretending a
missing parser proves absence. Browser packet processing still avoids originals.

JPEG extraction uses ExifTool `-ee` to inspect metadata beyond the first scan,
even when Pillow already finds early camera tags. Header-only JPEG packets no
longer qualify merely because they have Make/Model. A bounded complete marker
walk retains vendor application segments and verified SEFT trailers; supported
contiguous MPF JPEG image sets retain each child's metadata, not image pixels.
Unknown, incomplete or out-of-budget layouts remain fallback. The browser's
complete-walk threshold is four times its metadata payload budget; the existing
aggregate read bound (eight times that budget), 8 MiB packet cap and upload
concurrency are unchanged. Native JPEG/PNG fallback incurs a temporary copy and
ExifTool subprocess, without full raster decoding.

Trusted native-worker metadata also keeps all tags within the server's storage
limits, rather than passing through the browser's 200-key sanitizer. Zero GPS
coordinates remain valid; malformed DMS, invalid references, booleans, nonfinite
coordinates and incomplete pairs cannot be persisted as successful geo results.
Native storage truncation is an explicit `exif_storage_limit` failure.

One Samsung motion-photo packet still contains its embedded video block and is
about 4.47 MB. The 1.5 MB preview limit does not apply to metadata packets. Making
that transport smaller without losing nested metadata is a separate cost target.

## The 42 Exceptions Are Not Solved By Relabeling

Actual worker extraction reports 23 `exif_unsupported` and 19
`exif_parser_error`, not 42 successful absence checks. The affected extensions
are CRW, RAW, MRW, MDC, ARI and LRI.

Native LibRaw and the browser's LibRaw 0.22.1 open 35/42 without sensor unpacking.
None exposes nonzero ISO, shutter, aperture, focal length, timestamp or GPS data
through the inspected LibRaw capture structures. None has the TIFF/CIFF byte-order
header used by the supported metadata transports.

Thirty-three camera/size pairs match LibRaw's hard-coded file-size table exactly;
the other two sizes use its documented-in-source model-disambiguation branches.
This supports a headerless/legacy-layout explanation, not a claim that camera
identity was extracted from EXIF. See the versioned
[LibRaw identification implementation](https://github.com/LibRaw/LibRaw/blob/0.22.1/src/metadata/identify.cpp).
Inferred identity is not substituted for authentic EXIF. Seven files are not
opened by either tested LibRaw build. Other readers or external sidecars may
still contain information not established here.

Overall exact packet reuse remains **622/664 (93.67%)** when every source is in
the denominator. The measured 100% result applies only to the fixed, independently
enumerated 622-file extractable cohort. Do not market it as universal support.

## Reproduction And Evidence

Generate packets with `frontend/scripts/exif-corpus-packets.mjs CORPUS OUTPUT`.
Run `backend/scripts/audit_exif_corpus.py OUTPUT --output REPORT` for observations.
To enforce the target add `--baseline-index BASELINE/index.json
--baseline-report BASELINE_REPORT --require-target`. `--recheck-report REPORT`
rehashes sources and binds an existing complete extraction report to that cohort.
The raw exception probes are `backend/scripts/audit_exif_exceptions.py` and
`frontend/scripts/exif-exception-metadata.mjs`; neither unpacks sensor pixels.

Private, Git-ignored evidence under `.azure/forenklaprep-20261015/`:
`exif-hardening-baseline.json`, `exif-hardening-fixed.json`,
`exif-hardening-gated.json`, `exif-hardening-final.json`, `exif-hardening-fixed-comparison.json`,
`exif-hardening-worker-20261016/verification.json`,
`exif-hardening-exceptions.json`, `exif-hardening-libraw-metadata-gps.json`,
`exif-hardening-internet-*/audit.json`, and `exif-hardening-jpeg-v3/audit-final.json`. Packet files retain original metadata
privately; reports do not expose GPS values, capture text or credentials.

Real sandbox finalization/worker receipts and a repeated upload-throughput test
remain required before claiming deployed reliability or cost savings. Old
incomplete stored packets are not retroactively repaired; they remain fallback
candidates until regenerated. Preview limits, original-upload concurrency,
video's no-backend-fallback policy and the deferred GPR visual decoder are unchanged.
