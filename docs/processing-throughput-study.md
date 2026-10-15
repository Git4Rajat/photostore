# Processing Throughput Study

This instrumentation observes the existing pipeline. It does not tune scaling,
batching, timers, concurrency, retries, deduplication or video fallback behavior.

## Evidence

`processing telemetry {json}` records contain a schema version, UTC event time,
opaque process instance and replica identity. File/library IDs hash the library
and canonical filename; they do not expose names, EXIF, geo, credentials or URLs.
Browser-generated UUID `uploadBatchId` travels in finalize requests and normal
vision queue messages. It is also added to the existing metadata stamp, without
an additional storage operation. Join clustering and recovery events back to the
bounded upload run using the file ID. No per-library diagnostic cache is retained.

| Events | Interpretation |
| --- | --- |
| browser_batch | Authenticated, best-effort browser start/end counts; end waits for finalization tails. Uploaded excludes duplicates. Browser claims are not server receipts. A tab crash can omit the end event. |
| upload_finalized / upload_asset | Server finalization, media kind/bytes/duplicate outcome, bounded preparation claims and backend receipts. |
| worker_start / worker_import_complete / worker_ready | Worker invocation, app import/bootstrap wall time and poller readiness. Import time is separate and must not be added twice to model timings. Readiness does not assert every optional model loaded; inspect warm-phase errors. |
| worker_phase | Monotonic duration and outcome for model loading, queue accumulation, processing, index acquisition and checkpoints. Nested phases overlap: never sum them as independent wall time. |
| worker_state | Cumulative, exclusive startup/idle/accumulating/processing/checkpoint resident seconds; window CPU seconds and RSS. One record per minute and a final partial-window record on graceful exit. Hard kills may lose the final interval. |
| vision_received / vision_started / vision_attempt / vision_acknowledged | Queue residence age, local scheduling delay, attempt outcome and successful queue deletion. ACK is not evidence of productive inference. |
| vision_result | Productive completion proxy, step-error flag, face count, lease/apply/steps overhead. Require productive=true AND matching successful ACK for successful-photo throughput. |
| vision_step / vision_input / vision_download | Step latency/error and input kind versus actual payload download bytes. Input bytes can be reused across steps; sum only download events for transfer volume. |
| cluster_buffered / cluster_enqueued / cluster_received / cluster_started | In-memory buffer wait, queue wait and accumulation/preparation wait. |
| upload_activity / upload_heartbeat / cluster_upload_gate / cluster_buffer_flush | Media mix at init/finalize, persisted shared activity heartbeat and gate decisions/flush reasons. Mixed video activity can prolong the photo-clustering gate. |
| cluster_batch / cluster_prepare / cluster_assignment / cluster_storage | Effective work units, accumulation cutoff, metadata preparation, lease/prefetch time, transactions and persistence overhead. |
| cluster_applied / cluster_acknowledged / cluster_retry | Assignment attempt, ACK and redelivery evidence. Applied counts are attempts, not distinct successful photos. Faces are pending unique input IDs per photo on that attempt, not a claim about unique people or all historical faces. |

Existing `ipwork throughput metrics=` supplies per-replica slot occupancy,
preparation-only/ready/idle seconds, productive/error counts, payload bytes and
histograms. Its quantiles are bucket upper bounds, not exact sample percentiles.
Existing PERF scopes supply storage transactions, time, bytes and duplicate I/O.
Existing FAISS logs distinguish checkpoint restore from cold rebuild.

`worker_first_work` means the first dispatched task, possibly a no-op; use the
first productive result plus ACK for time-to-first-success. Worker processing
state includes storage waits, not just CPU inference. Compare CPU and PERF spans
to distinguish compute from I/O. Cluster accumulation includes empty polling;
received/batch events distinguish waiting for the first message from holding work.

## Study Protocol

1. Record bounded start/end UTC timestamps, selected file mix, app revisions,
   effective settings, library and existing library size. Avoid concurrent
   unrelated uploads to the same library. Do not bypass deduplication.
2. Retain files until the audit is complete. Inspect missing events as unknown,
   never silently convert missing telemetry to a zero or a success.
3. Correlate file/attempt/message IDs before counting successful files. Use
   exact grouping for study totals, rather than approximate dcount or counting
   queue receives. Separate no-ops, step failures, missing derivatives and retries.
4. Report unique successful photos/sec across upload-to-completion and per busy
   replica-second; also per-stage distributions. For clustering report photos/sec
   and pending faces/sec, plus its eligible denominator (photos with faces).
5. Overlay Container Apps system lifecycle events and one-minute Azure metrics
   for replicas, UsageNanoCores, WorkingSetBytes, RestartCount, RxBytes and TxBytes.
   Platform start/pull/scheduling and scale-down tails are outside Python's
   lifetime. Minute-averaged replica metrics yield estimates, not exact billing.
6. Keep minReplicas, maxReplicas, queue targets, cooldowns and batching unchanged
   for the first baseline. Compare matched cold/warm photo-only, mixed and
   video-only runs, then change one setting at a time in repeated trials.
7. Expected video-only behavior: no upload-triggered vision or face-clustering
   work and no backend derivative/EXIF fallback. Library indexing or maintenance
   can still run. Do not attribute unrelated maintenance to video inference.
8. Choose the lowest allocated vCPU/GiB-seconds per successful photo that meets
   a specified completion-latency target. CPU busy seconds are not billable
   allocated seconds. Validate estimates against actual Azure usage/cost meters;
   shared free grants and Log Analytics ingestion affect the final bill.

## Privacy And Overhead

The telemetry endpoint requires a signed-in library context, accepts at most
4 KiB, validates UUIDs and fixed numeric fields, and writes logs only. It cannot
queue work or alter a photo. Browser summary delivery never blocks uploads.
Diagnostic worker threads report once per minute and stop on shutdown. No new
worker storage lookups, queue probes, model calls or content downloads are introduced.
The two browser-summary requests use the existing authentication/membership checks.
Per-file events add logging/ingestion cost; include that overhead in the study.

Queries: [processing-study.kql](../deploy/diagnostics/processing-study.kql).
