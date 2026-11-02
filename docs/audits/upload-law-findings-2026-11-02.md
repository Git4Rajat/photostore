# Upload LAW investigation — 2026-11-02

Read-only inspection of Log Analytics workspace
`6ca9803c-dd23-4393-a80a-c1fe035e7df9`. Query interval:
**2026-11-01 22:31–23:10 UTC**, or **November 1, 23:31–November 2, 00:10
Stockholm time**. The upload app still reports a succeeded deployment of
commit `8911bf990375bcaca56ede78bbfd8e3a974087f8`, revision `0000052`.

No application code, Azure configuration, storage rows or queue messages were
changed. Raw logs are retained only in temporary local files; this document
does not include filenames, signed URLs or source metadata.

## Completed browser batch

The browser reports one 3,491-photo selection, started at 22:32:14 UTC and ended
at 23:01:34 UTC (00:01:34 Stockholm time on November 2):

| Counter | Value |
| --- | ---: |
| Selected | 3,491 |
| Uploaded | 3,395 |
| Duplicates | 94 |
| Failed | 2 |
| Browser elapsed | 1,760.718 seconds (29m 20.7s) |

These are browser counters, not a fresh storage inventory. Server telemetry logs
3,599 finalized file attempts across 3,480 distinct library/file identifiers,
including 119 duplicate attempts. Retries mean server attempt counts cannot be
used as unique uploaded-file counts. The browser outcome is not evidence of
downstream Vision/index completion.

## Request and storage evidence

The outer request logs contain:

| Route | Requests | HTTP results | Median / p95 app time |
| --- | ---: | --- | --- |
| Init batch | 1,631 | 1,630 x 200; 1 x 503 | 591 / 2,107 ms |
| Finalize batch | 1,091 | 1,046 x 200; 45 x 503 | 8,085 / 28,901 ms |

After 22:34 UTC, excluding the earliest scale-up period, finalize still has
1,065 requests, 44 HTTP 503s and median/p95 **7,958 / 29,141 ms**. Cold start
does not explain the persistent bottleneck. These server timings exclude
upstream queueing seen in the HAR.

Finalize requests account for **866,381 instrumented storage calls** across
3,646 submitted file attempts, about **237.6 calls/file** (238.4 after 22:34).
Request-level top-operation summaries expose these lower bounds:

| Operation | Observed calls | Calls/file attempt | Attributed ms/file attempt |
| --- | ---: | ---: | ---: |
| Jobs-table GET | 383,131 | 105.1 | 1,172 |
| Table batch transaction | 168,376 | 46.2 | 665 |
| Search-dirty-table GET | 157,497 | 43.2 | 498 |
| Metadata GET | 58,234 | 16.0 | 198 |
| Metadata PATCH | 19,826 | 5.4 | 76 |

The first three account for roughly **82%** of observed finalize storage calls.
The operation summaries contain only each request's top operations, so table
counts are lower bounds. Storage elapsed times can overlap and are not CPU
measurements. Nested phase totals must not be added.

Duplicate-I/O warnings independently report 332,088 observed repeated jobs GET
calls across 7,855 warnings and 18,158 repeated search-dirty GET calls across
329 warnings. These are warning tallies, not a count of removable calls:
some repeated reads deliberately revalidate mutable fences.

## Finding 1: automatic coverage bookkeeping causes real failures

Three per-file finalize errors, with error records at 22:48:09, 22:50:57 and
22:51:54 UTC, trace through `automatic_index_updates.SourceTable.write` into
coverage `begin()` or `finish()` and `CoverageStore._change`. The exception is
Azure Table `UpdateConditionNotSatisfied` / `TableTransactionError`.

Four inline-processing errors (22:42:43, 22:49:56, 22:50:40 and 22:51:01 UTC)
have the same coverage transaction cause. Some failed completion attempts are
reported as `ExceptionGroup: Automatic source finalization failed; retry required`.
They occur on ordinary metadata result persistence and the batched-status flush.
Thus this is more than speculative overhead: the coverage layer both consumes
storage operations and fails under contention. Three failed file attempts and
two final browser failures are different counters; do not claim a one-to-one
mapping without request/file correlation.

Relevant code: `automatic_index_updates.py:488` wraps every source write with
admission/target checks and coverage begin/finish; `_change` retries conditional
transactions up to 20 times without backoff. Wave-owned writes suppress the
maintenance schedule but still reserve and finish coverage. Each separate
metadata update repeats that work.

**Recommended change:** consolidate browser preparation's aggregate metadata,
step-status and lease updates into a generation-aware conditional commit, then
reduce duplicate coverage work per logical source mutation. Add bounded jitter
to coverage transaction conflicts. Preserve pre-write durable reservations and
cleanup checks; do not bypass the coverage wrapper or cache operator fences.
Completion failures also warrant an audit of remaining reservations: active
coverage reservations intentionally do not expire. A blind retry that opens a
new reservation is not a substitute for safely completing/recovering the old
one. LAW alone does not establish current storage ledger state.

## Finding 2: wave control contention is a separate bottleneck

There are **55 exhausted wave mutation retries**, each with 16 attempts:

- 45 `ShadowWaves.resolve.run`, all HTTP 412 at transaction index zero.
- 10 `ShadowWaves.register.run`: nine at index zero, one at index one.

Their recorded retry-loop durations total 262.3 seconds across concurrent
requests, not 262.3 seconds of wall-clock upload delay. All 45 finalize HTTP 503s
correspond to exhausted source-resolution contention. `_commit` places the
shared library control update at transaction index zero.

`routes/upload_wave_live.py:102` resolves files individually after the route;
`upload_waves.py:306` commits control/session/wave changes for each file.
Registration competes on the same shared state.

**Recommended change:** introduce bounded, idempotent batch admission/resolution
transactions grouped by session/wave, reducing shared control revisions per
batch while preserving each source's binding, partial-result semantics,
cleanup fences and Azure transaction limits. Avoid simply increasing frontend
concurrency or replica count: both can increase shared-row contention.

## Finding 3: browser result application uses multiple persistence passes

The browser path in `storage_utils.py:11008` retains the legacy result upsert,
batched-status flush (`11674`) and final metadata/lease refresh. The worker path
already has a consolidated result commit, but its lease contract cannot simply
be reused for unleased browser uploads.

Inner finalize timing averages per submitted attempt include client processing
1,368.5 ms, persistence 1,282.3 ms, finalize metadata 872.2 ms, metadata packet
280.2 ms and metadata stamp 255.3 ms. They nest and cannot be summed. The
multiple browser writes amplify Finding 1, so combining them is preferable to
optimizing another isolated GET.

There are 3,602 `embedding_write` phase records: average **202.35 ms wall time,
34.92 ms thread CPU, 13.79 SDK calls and 162.91 ms SDK send time**. Semantic
refresh itself averages only 1.75 ms wall time and 0.40 ms thread CPU. Only two
original-image fallback downloads are logged, totaling 11,338 bytes.

**Recommended change:** inspect whether browser EXIF/derivative-only updates
need an embedding write at all, and retain supplied valid vectors while leaving
model-owned work to its owner. This is a smaller follow-up than consolidating
coverage and metadata commits. These measurements do not support treating
image download, image decoding or semantic computation as the main bottleneck.

## Measurement correction

All 1,091 `finalize_batch_fanout` records report zero queue sends. Their
`job_table_writes` field is misleading: `routes/upload.py:849` counts every jobs
table operation, including GETs, not just writes. It reports 318,575 operations
over the interval; this must not be described as 318,575 job writes or queue
enqueues. Correct that metric before using it to evaluate the next change.

## Order of work

1. Consolidate browser result persistence and reduce coverage begin/finish
   contention, including safe completion/recovery behavior.
2. Batch wave admission/resolution to reduce conflicts on shared control rows.
3. Remove unnecessary embedding persistence and fix the misleading counter.

Compare complete selections on a warmed service, using files/attempts,
storage calls/file, application and end-to-end p95, exhausted conflict counts
and final browser failures. No performance gain is claimed for an unimplemented
change, and no live remediation has been performed.

A cross-role LAW check after the browser batch ended (23:01:35–23:10 UTC)
found no matching active-coverage, unbalanced-reservation or automatic-finish
error messages. This absence of log messages does not establish that stored
coverage counters are balanced; no coverage rows were read or modified.
