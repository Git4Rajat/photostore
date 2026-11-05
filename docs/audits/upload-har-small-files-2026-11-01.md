# Small-file upload HAR — 2026-11-01

## Scope and limitations

Local inspection of `download.devtools` supplied by the user. No cloud queries,
deployments, or application changes. Existing unrelated workspace changes were
left untouched.

The export is not valid complete JSON: it ends inside the final entry's string.
Sequential JSON decoding recovered **17,260 complete entries** before the
incomplete entry at character offset 392,823,690. Findings below describe this
partial, in-progress capture, not the whole selection or its final outcome.
The captured interval is approximately **22:22:45–22:39:23 Stockholm time**
(21:22:45–21:39:23 UTC), 997.6 seconds including request completion.
Do not commit the raw capture: it includes signed blob URLs and private payloads.

## Measurements

| Operation | Calls | Median | p95 | HTTP results |
| --- | ---: | ---: | ---: | --- |
| Wave item registration | 692 | 189 ms | 2,251 ms | All 200 |
| Init batch | 692 | 903 ms | 2,639 ms | All 200 |
| Finalize batch | 601 | 6,885 ms | 13,494 ms | All 200 |
| Original block PUT | 1,211 | 55 ms | 130 ms | All 201 |
| Original block-list commit | 1,213 | 50 ms | 114 ms | All 201 |
| Other blob PUTs | 3,635 | 52 ms | 120 ms | All 201 |

HTTP 200 alone does not establish per-file success or downstream completion.
Percentiles use the sorted observed durations, without interpolation.

Init responses cover 1,212 file entries: 342 batches of one, 180 of two, and
170 of three, for **1.75 files/request**. Finalize responses also cover 1,212
entries: 228 batches of one, 145 of two, 225 of three, two of six, and one of
seven, for **2.02 files/request**. Those response counts are attempts, not an
independent inventory of stored photos.

The 1,212 captured finalize request items have distinct SHA-256 values and
report **65.15 MB** of source data. The source-volume rate over this capture is
approximately **73 files/minute and 0.065 MB/s**. This is application progress
for the captured interval, not available link bandwidth. It includes bytes
whose upload may have started before capture began. HAR bodySize accounting
for binary payloads differs substantially from these declared source sizes;
do not use it as a reliable source-volume estimate.

Finalization is slow even at size one:

| Files in finalize request | Calls | Median request duration |
| ---: | ---: | ---: |
| 1 | 228 | 4.55 s |
| 2 | 145 | 7.99 s |
| 3 | 225 | 7.86 s |

Init batches of one, two, and three have medians 915, 983, and 890 ms,
respectively. Finalize median remains approximately 6.3–7.2 seconds in
successive three-minute windows; this is sustained, not just initial warm-up.

Init and finalize browser blocked-time medians are 0.3 and 0.4 ms. Their time
is almost entirely `wait` (time to first byte), which includes network transit,
upstream queueing and server work; the HAR cannot distinguish those components.
Average outstanding init requests are 0.82 (peak three); finalize requests
average 4.38 (peak twelve), over each operation's observed interval.

The union of blob PUT intervals covers only **183.5 seconds** of the roughly
998-second capture. This is wall time with at least one PUT in flight, not a
sum of concurrent request durations. There are gaps as long as 37.6 seconds.
Upload-service POSTs cover 785.7 seconds, with a largest idle gap of 32.7 seconds.
These gaps prevent attributing all elapsed time to finalize alone.

Browser preparation reports have median/p95 elapsed times **315/605 ms** and
admission waits **0/5 ms**. All 1,212 captured finalize payloads report a browser
ExifTool EXIF step. The capture contains 1,212 metadata-worker and 1,211 raster
worker resource entries; status zero on these entries must not be interpreted
as proven worker failures. Worker startup/reuse is a secondary opportunity,
not the dominant measured request delay.

## Code implications and order of work

1. **Reduce per-file finalization storage work first.**
   `backend/routes/upload.py:633` loops through files sequentially. Batching
   amortizes request-level work, but still performs validation, blob properties,
   metadata finalization/stamping/reads, receipts, processing queue work and
   live-wave admission per file. Match this interval against the existing
   `UPLOAD_TIMING` and outer `PERF event=request` diagnostics before selecting
   storage optimizations. The HAR cannot prove which internal stage dominates,
   whether Table contention occurs, or how many replicas are running.
2. **Make batching effective under staggered arrivals.**
   `frontend/src/components/AppServicesProvider.tsx:2274` caps finalize batches at
   24, but a resettable 200 ms debounce flushes as soon as an interarrival gap
   exceeds 200 ms. The 2,500 ms max-wait is an upper bound, not a minimum
   collection window. Therefore raising only the cap will not fix these
   mostly one-to-three-file requests. Evaluate bounded collection windows or
   service-time-aware coalescing, with an explicit tail flush and cancellation
   behavior. Measure per-file cost as well as HTTP count: larger sequential
   batches alone do not remove that cost and can occupy server threads longer.
3. **Remove avoidable small-original round trips.**
   The current path stages one block then commits a block list even for tiny
   originals. A bounded single-PUT path could remove one PUT and its preflight
   per such original. Preserve retry, abort, blob ownership, progress and resume
   semantics. This addresses request count, but does not by itself eliminate
   the measured multi-second finalization delay.
4. **Review blob CORS preflight reuse and worker startup.**
   Each of the 6,059 observed blob PUTs has a corresponding OPTIONS entry.
   Inspect the actual CORS configuration and signed-URL behavior before changing
   caching. With unique per-blob URLs, increasing max-age alone may not help.
   Preparation worker reuse may save startup overhead, subject to decoder memory
   and failure isolation requirements.

Do not infer that more replicas, more frontend concurrency, or a larger batch
cap will solve this capture. There is no evidence here of raw link saturation,
and no server trace establishing that extra concurrent work is safe or useful.
No performance improvement is claimed without a comparable follow-up capture.

## Local optimization implemented

The follow-up fix changes application code, without changing resource sizes,
thread counts or deployment settings:

- `wave_policy.finalization_item` scopes the internal metadata-finalization write
  checks to the file being written. Previously each of those checks reread every
  admitted reservation. With two checks per file, a batch of N files performed
  `2*N*(N+1)` policy/reservation reads; scoped checks perform `4*N`.
  At N=24 this is **1,200 -> 96 reads** for these checks alone. This is a
  deterministic operation-count reduction, not a measured production speedup.
- Each check still fetches current cleanup policy and the current reservation.
  Admission validates the complete batch. Batch initialization remains fenced
  against all its bindings. The single-file scope resets on exceptions, rejects
  bindings outside the admitted batch, and does not cache mutable safety state.
- Frontend finalization collects for a fixed **1,000 ms** from the first ready
  file instead of a resettable 200 ms debounce. The 24-file cap, same-name
  separation, dispatch gating and retries remain in place. A final isolated
  file now waits up to one second for collection when capacity is available;
  original-file transfers proceed independently.

Validation: **222 backend tests** covering upload diagnostics, live routes,
write/deletion fences, session leases, init batching, RAW finalization,
preparation and video uploads; **71 frontend upload tests**; TypeScript
`tsc --noEmit`; and `git diff --check` all pass. The new staggered-arrival
regression verifies that three files ready 350 ms apart are dispatched in one
request by the original collection deadline. Read-count tests cover batch sizes
1, 3 and 24; mutation tests cover cleanup, GC, cancellation and lease expiry.

No deployment was performed. Compare another upload of the same dataset after
rollout for finalize latency, files/minute, per-file storage calls and per-replica
CPU. These changes remove proven redundant work; they do not establish the
remaining CPU profile or eliminate every per-file storage round trip.

### Follow-up: metadata reuse and single-PUT originals

Both single and batched finalize routes now use the entity returned by the
conditional metadata stamp instead of immediately fetching it again. A `None`
result or exception retains the original read fallback. Hash-mismatch rejection
still uses the resulting metadata. This removes one redundant metadata read per
successful stamp; no cross-request cache is introduced.

Fresh nonempty originals no larger than 1 MiB (and no larger than their configured
staging block) now use `BlockBlobClient.upload` for one PUT instead of staging one
block and committing a block list. Existing upload IDs or block checkpoints retain
the block path. The PUT preserves content type, cancellation, bounded transient
retries and slow-transfer heartbeats. Progress is credited after commitment. An
abort verifies committed size with a signal-free properties read before allowing
finalization, sharing the existing block-commit recovery behavior. Retried/resumed
files can fall back to the established block path.

The retry classifier now recognizes the SDK's `statusCode` and
`REQUEST_SEND_ERROR`, alongside the existing API error shapes. New regressions
cover the one-PUT limit, larger-file block uploads, transient PUT failure, aborted
PUT verification, heartbeat shutdown, and metadata read fallback/hash checks.
These changes remain local; no deployment or production speedup is claimed.

Follow-up validation: **230 backend tests, 78 upload UI tests, TypeScript
`tsc --noEmit`, and `git diff --check` pass**.

### Follow-up: receipt consolidation and worker reuse

Receipt generation now runs as a derived-fields callback within
`_update_metadata_entity_fields`. The normal receipt persistence path drops from
**two metadata reads plus one conditional write to one read plus one conditional
write**. On a write conflict the helper reads the latest row and recalculates the
receipt, so it does not overwrite a concurrent processing result with a receipt
based on an older snapshot. Receipt events are emitted after the update succeeds;
diagnostic failure still does not reject an uploaded original.

Healthy raster-preparation and photo-metadata workers are reused across files.
Each pool retains at most two idle workers and terminates them after 15 seconds
without work. The existing callers still bound concurrent jobs. Failed,
cancelled or timed-out workers are discarded; idle worker errors evict the
worker, and pipeline disposal terminates retained workers. Late callbacks from
cancelled/retried jobs cannot settle subsequent jobs. RAW/HEIF derivative workers
and video metadata workers retain their one-shot lifecycle to release decoder
heaps rather than retain them speculatively.

A regression processes ten sequential raster jobs with one worker creation.
Other regressions cover independent metadata packets, idle expiry/capacity,
disposal, cancellation, timeout, worker errors and receipt recalculation after
ETag conflict. The single-PUT size limit remains unchanged. No deployment or
production CPU/throughput measurement has been performed.

Validation for receipt consolidation and worker reuse: **432 selected backend
tests, 166 frontend upload/preparation/pool tests, TypeScript `tsc --noEmit`, and
`git diff --check` pass**.
