# Vision inspection, persistence and OCR reuse

The instrumented `b288f7bad916408d8d80bd237372d943` run included 2,162 members,
286 accepted uploads and 1,876 duplicate references. Its source summaries showed
about 100 seconds of accumulated inspection, 182 seconds of result application,
and 21.5 seconds of OCR initialization against 11.2 seconds of recognition.
Those are overlapping operation totals, not additive wall-time slices.

## Changes

1. `InspectionBatch` loads page-local deletion/supersession/settlement witnesses
   and completion inboxes using bounded key queries, then prefetches immutable
   manifests and face snapshots. Every completion, source binding, generation,
   contract and receipt digest is still validated. Mutable snapshots select work;
   source claims and writes retain their authoritative fences. Post-source
   qualification discards the inbox/proof snapshot and rereads source state so a
   newly deleted/superseded source is settled accordingly. Relay reads the inbox
   again after delivery, rather than reusing a missing prefetch result.
2. Face persistence avoids a duplicated metadata check when the immediately
   following conditional artifact writer performs that check itself. Filename
   generation/lease checks remain. If the filename lease actually needs extending,
   that write still gets its own source check. Original artifact ETags, create-only
   writes, and final filename/metadata publication checks remain unchanged.
3. Immutable face snapshots use create-only transactions of up to 20 rows, before
   manifest publication. A 409 rolls back the whole batch; fallback individual
   creates verify existing payload equality. Other failures propagate. Snapshot
   face/embedding reads use bounded queries for photos with at least four faces;
   small photos retain point reads to avoid query overhead.
4. OCR uses resident daemon workers with thread-owned Tesseract engines. Successful
   calls reuse trained data, clear image/results and adaptive state between photos,
   and retain no previous request closure while idle. Errors retire the worker.
   A timeout permanently retires that worker and replaces it within a bounded pool
   (`IPWORKER_CONCURRENCY + 2` total resident/retiring workers). Native calls are
   never killed; their independently owned images close only after recognition
   returns. Shared source pixels are copied once for OCR so source-job cleanup
   cannot close pixels still used by a timed-out native call. Engine cleanup
   runs on the owner thread. If all slots are occupied by stuck native calls, new
   requests return an explicit engine-capacity failure instead of creating unlimited
   threads. A process restart clears native hangs.

## Verification and measurement

The deterministic 20-completed-source fixture preserves exactly the same inspected
results while replacing **80 job/artifact point reads with seven bounded queries**.
It excludes metadata prefetch, stage guards, final evaluation and network latency;
this is a request-count comparison, not a production throughput prediction.

Two-face persistence retains six authoritative source checks: filename acquire,
embedding/face writes for each face, and filename publication, instead of the ten
checks that included duplicated renew/write checks. Source replacement during an
embedding read still blocks every artifact write.

Tests cover receipt tampering, create conflicts, partial batch failure, source
supersession, stale ETags, filename lease loss, rolling concurrency and deadlines,
OCR output parity, owner-thread cleanup, engine reuse, timeout replacement and
bounded replacement capacity. A real Tesseract text/blank/text sequence also runs
when its native dependency and English data are available.

Use attempt summaries to compare the next upload:

- `vision_inspection_prefetch_ms` / `vision_inspection_prefetches` / errors:
  prefetch overhead, separate from per-image inspection timers. Compare their sum
  with earlier inspection plus receipt I/O; do not call a moved duration a saving.
- `job_read_requests`, `metadata_point_reads + metadata_query_requests`,
  `batch_queries`, `storage_calls`: actual request reductions, including extra
  fresh post-source state checks.
- `receipt_write_batches`, `receipt_creates`, `receipt_create_conflicts`:
  snapshot batching and replay behavior.
- `ocr_worker_starts`, `ocr_worker_reuses`, `ocr_worker_timeouts`,
  `ocr_worker_capacity_errors`, `ocr_engine_inits`, `ocr_engine_init_ms`,
  `ocr_recognition_ms`: reuse versus initialization, native work and degraded workers.
- Source slot occupancy, source steps/apply time and end-to-end Vision freeze time.

Normalize by fresh source work and duplicate/receipt reuse, not just wave member
count. The algorithms, model weights, OCR confidence threshold and face quality
filters have not changed. No telemetry-only storage operations are introduced.
