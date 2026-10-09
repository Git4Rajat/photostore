> **2026-10-09 role rename**: `ipworker`→`vision` (see `deploy/resources.bicep`/
> `backend/app.py`). This doc still says `ipworker`.

# Bounded ipworker face-index reconciliation

## Purpose

Revision 0000024 diagnostics showed filename-query enumeration averaging 7.20s
and consuming 88.5% of message time. Every measured photo used a missing-index
fallback returning no rows. Missing is still **unknown**, not evidence of zero.
This change amortizes that authoritative enumeration across a bounded wave;
it does not change the face data model or supply immutable upload identities.

## Runtime contract

- `IPWORKER_FACE_RECONCILE_BATCH_SIZE` defaults to **8**, clamped to 1–32.
  Set **1** to restore the original receive/refill path with no preparation.
- The queue thread receives at most one wave, then prepares it on one separate
  thread. It does not receive another wave until that wave has finished.
- At most the configured wave size of messages is retained across preparation,
  ready, and processing states. Only `IPWORKER_CONCURRENCY` photo tasks execute
  concurrently. Preparation fetches no image bytes or embedding vectors.
- Malformed, poison, non-face, missing/deleted, and already-current face jobs
  are excluded from preparation. Normal processing still checks actual leases,
  fresh statuses/model versions, and the ordinary mutation safeguards.
- Eligible names are grouped by library. Existing schema-valid complete indexes
  are untouched. Unknown names acquire filename generations before any scan.
- One query per unknown-name group projects only `RowKey` and `filename` and
  streams every page. For up to 14 unknown names it uses a filename OR filter:
  Azure Table permits 15 comparisons, including the library partition key.
  This remains a server-side partition scan, **not a keyed lookup**. It avoids
  returning the whole library and shares enumeration across the group.
- Groups of 15–32 unknown names use a streamed partition query, retaining only
  their matching IDs. Prefer the default 8: larger groups can transfer more
  data. Retention caps are 4,096 IDs/photo and 8,192 IDs/group; exceeding them
  fails closed. No full-library row cache is constructed.
- After exhaustion, validate all held generations and publish each actual ID
  set with generation/ETag CAS. Explicit zero is now verified by the scan.
  Normal photo processing subsequently acquires the index and point-reads
  fresh face rows, preserving rejected/confirmed/propagated/named identities.
- Acquired generations become dirty on scan, lease, cancellation, bounds or
  publication failure (or remain incomplete if the cleanup itself fails).
  Preparation failure never ACKs a message and never fabricates a zero. The
  entire wave is deferred for queue redelivery; it does **not** immediately fan
  out per-photo recovery scans into all inference slots.

## Queue visibility and shutdown

All queue receive/delete operations remain on the main thread. Preparation
never ACKs: only a normal per-message outcome can reach the existing ACK rules.
Processing errors remain unacknowledged for retry.

Preparation has a cooperative deadline of the smaller of 120s or one quarter
of the queue visibility timeout. It checks cancellation between acquisition,
pages, returned rows and publications. Messages are not started after half
their original visibility timeout; unstarted jobs are left unacknowledged and
redeliver normally. Standalone batch reconciliation also has a 90s cooperative
budget. Both batch and per-photo fallback queries use a 10s service timeout,
5s connection timeout, 15s socket read timeout, and zero SDK retries. These are
**per request/page**, not an absolute deadline spanning the whole query.
Per-photo fallback enumeration checks a 90s overall budget on pages, rows and
exhaustion, and renews the filename lease every 30s, including empty pages.
Partial/expired enumeration cannot authorize source mutations or verified zero.

A blocked SDK/native call cannot be killed safely as a Python thread. The main
loop checks preparation against its cooperative deadline plus 20s, and photo
tasks against `IPWORKER_TASK_TIMEOUT_SECONDS` (default 120s, clamped 1–240s and
further limited to visibility timeout minus 30s). An overrun dumps thread stacks,
shuts executors down without waiting, and exits the **process with status 1**.
It never replaces the blocked thread or ACKs unfinished jobs. Container restart,
queue redelivery and expired filename/processing leases provide recovery.
Checks run at polling-loop boundaries; main-thread queue/rebuild IO and startup
model warming are not covered by this background-task watchdog. Socket timeouts
and cooperative budgets alone are not a universal hard wall-clock guarantee.

On shutdown no new wave is received, ready messages are abandoned unacknowledged,
preparation is cancelled cooperatively, and only active photo tasks are drained.
The original per-photo processing lease/visibility limitations are unchanged;
this is not a new visibility-heartbeat protocol.

## Diagnostics and validation

`face index batch timings metrics=...` reports requested/indexed/reconciled
counts, scan filter mode, pages, rows **returned**, retained IDs, query and total
milliseconds, and outcome. Rows returned do not represent server-side rows
examined; page counts are not SDK HTTP retry counts. New aggregate records
contain no filenames, payloads or credentials.

`ipwork face batch prepared ...` reports preparation size and duration.
`ipwork face batch deferred ...` distinguishes preparation-failure/shutdown/visibility-budget
deferrals from processing failures. Existing throughput counters still count
normal completion/ACK independently; `in_flight` represents photo tasks, not
unstarted preparation messages.

`ipwork watchdog timeout ...` identifies stuck preparation/tasks and pending
counts. `face storage timings` includes `query_pages` for fallback enumeration.

### Revision 0000025 stall investigation

All four replicas reported preparation cancellation around 75s. They then
launched per-photo fallback queries taking 162–250s and failed when their 120s
filename leases expired. Throughput counters continued, and later messages
started: this proves a severe scan-driven stall, not a permanent preparation
future deadlock. The separate clustering worker was observed starting a FAISS
cold build; that record does not establish the same failure there. These limits
and deferrals prevent the fallback cascade, but do not make non-keyed partition
scans fast or certify the 10,000-new-photos/hour target.

After rollout compare:

1. Batch query time per reconciled photo and actual photos/hour on new work.
2. `face storage timings` moving from `filename_query` to `indexed_point_reads`.
3. Failure/dirty recovery, deferred messages, ACK failures and peak memory.
4. Per-library mixed workloads, no-op-heavy queues and shutdown behavior.

No live throughput guarantee is made. A single slow scan gates a wave, and
already-indexed/non-face workloads can lose some refill overlap; size 1 is the
rollback. The permanent fix remains keyed per-photo canonical storage with
verified legacy coverage and immutable asset-generation fencing.