# Restore batching inside the Vision stage — 2026-10-26

## Problem and scope

The wave Vision consumer called the source adapter serially, reread source
metadata for eligibility, renewed the stage lease repeatedly for each image,
and wrote an observation/control update plus a checkpoint for every member.
The legacy face-lookup batch preparation ignored wave messages. The configured
worker concurrency therefore did not create parallel image processing inside
one wave.

This change keeps the existing stage order and publication barrier. It does
not change replica floors, scaling rules, model versions, or the one-attempt
source budget. It is a local implementation; no new worker image was deployed.

## Implementation

- Fetch eligibility metadata for one bounded member page at a time. Split OR
  queries into at most 14 row comparisons plus the partition comparison, and
  bound encoded filter size for long filenames. Do not cache mutable source
  rows after processing starts. Source claims and result commits still read
  authoritative metadata. The comparison bound follows the [Azure Table query
  contract](https://learn.microsoft.com/en-us/rest/api/storageservices/querying-tables-and-entities).
- Restore `reconcile_face_filename_indexes_batch` before processing each page,
  for current sources whose face step is still pending. Cancellation/deadline
  checks precede source-attempt admission.
- Run a bounded group of independent sources concurrently. All wave deliveries
  share one source pool sized by `IPWORKER_CONCURRENCY`; groups cannot exceed
  that configured limit. Copy the stage context and processing trace to source
  threads, and preserve the worker's timing collector.
- Reserve a group's attempt admissions in one transaction. Create-only writes
  prove first admission without individual point reads; a collision rolls back
  the whole transaction and rereads the actual counters. Previously admitted
  unqualified sources do not get another model attempt. Crash-after-admission
  may consume an attempt even if inference had not started; no success is
  fabricated in that case.
- Reuse immutable manifest/face snapshot reads within one page. Every reference
  is still validated; missing artifacts, metadata, policy, inboxes and leases
  are not cached. Source result/derivative/face writes keep their existing
  per-source generation and ownership fences.
- Apply member observations with one control/wave/ownership transaction per
  page. Reuse page ETags initially; on conflict reread and preserve concurrent
  terminal settlements. Checkpoint only drained groups, after observations are
  durable. A crash between source completion and the page checkpoint replays
  qualified receipts without running models again.
- Renew the stage lease on a bounded cadence, with its existing keepalive for
  blocking operations. Durable ownership and release checks are retained.
  In-flight source tasks drain before stage ownership is released.

## Validation

The focused shared-checkout suite covers source concurrency, generation/lease
fences, receipt validation, failure settlement, deadlines, page replay and CAS
conflicts. Earlier in validation, concurrent recovery-removal edits prevented older
integration tests from importing `wave_recovery.recover`. The matching test
updates subsequently landed in the shared checkout; the final focused command
passes there as well.

An isolated copy of commit `7e76cff`, overlaid with only this batching change,
validates the broader wave/processing integration suites without changing or
reverting the other edits. Exact command and final totals are recorded below.

The reproducible synthetic comparison is
`backend/scripts/benchmark_wave_source_batch.py --baseline-ref 7e76cff --photos 80 --model-ms 100 --storage-ms 2`.
It uses real wave/receipt logic with ETag test tables, fixed synthetic I/O/model
waits, and no cloud writes. Results are in
[`wave-vision-batching-20261026.json`](../benchmarks/wave-vision-batching-20261026.json).
Both paths must qualify all 80 sources. This measures coordination differences;
it does not establish decoding/model performance or 10,000 new images/hour.

Production acceptance still requires a representative upload through the new
image: unique durable image completions, stage publication, source failures,
CPU/memory use, and sustained throughput on the intended resource allocation.

### Recorded results

- Shared checkout: **136 passed, 5 skipped** across source batching, Vision
  consumers/liveness/activation, prepared derivatives, execution leases,
  processing receipts, and completion tests.
- Isolated committed baseline plus batching: **485 passed, 7 skipped** for
  `test_wave*.py`, `test_upload_wave*.py`, and `test_processing*.py`.
- `git diff --check`: passed.
- Synthetic 80-source comparison: **18.6581s → 8.9901s (2.08×)**. Both paths
  verified all 80 sources. Observation transactions **80 → 4**; checkpoints
  **81 → 6**; wave-table transactions **579 → 219**; wave-table point reads
  **2,831 → 1,819**. Eligibility metadata changed from **240 point reads** to
  **8 bounded queries plus 80 fresh post-processing reads**.

No production throughput result is claimed. The current image must be rebuilt
and deployed before these changes affect live Vision processing.
