# Bounded ipworker performance metrics

Each replica emits `ipwork throughput metrics=<JSON>` after at least 60 monotonic
seconds, plus a forced final sample before a watchdog/grace-period force exit.
This is a **tumbling window**, not a rolling window. Emission is opportunistic
from the queue loop: a blocked queue receive/delete can delay a sample. Windows
use actual elapsed time, not an assumed 60 seconds. There is no telemetry thread,
new dependency, new storage read, or new queue request.

## Identity, scope and memory bounds

`identity` contains only the allowlisted container app name, revision, replica
name and hostname (each truncated to 128 characters). Unavailable values are
empty. `config` records effective concurrency, face reconciliation batch size,
visibility/lease/task/grace timeouts, retry limits, polling period, registered
processors and face embedding version. It does not dump the environment,
credentials, service URLs, tokens, filenames, tenants or job IDs.

The collector has fixed counter and timing-name allowlists. Unknown names are
ignored. Each timing has two fixed-size histogram arrays (window/cumulative),
with one overflow bucket; no raw samples or distinct-ID sets are retained.
Receipt timestamps are held only for the bounded in-flight futures. State,
counter updates, histogram updates and resets share a lock, and worker-thread
instrumentation is scoped with thread-local context. API/browser processing
calls outside this worker do not contribute.

These are process-local aggregates; restarting a replica resets them. Existing
per-file timing/error logs and per-user rebuild accounting are unchanged; the
boundedness claim applies to the new aggregate collector, not all application
state or logs. Linux image copies already include all backend Python modules.

## Outcomes versus productive work

Both `window` and `cumulative` retain `done`, `noop`, `lease_busy`, `error`,
`not_found`, receive/received/receive-failed and ACK/ACK-failed counters. Existing
receive/ACK duration sums and `done_per_hour` remain available.

| Counter | Meaning |
| --- | --- |
| `done` | Handler finished its existing apply/job-status path. **Not** proof every processor succeeded, or the upload is new. |
| `retry_exhausted` | Dequeue count exceeds max retries. Job is marked failed best-effort; message is ACKed, but is not `done` and does not count toward index-rebuild milestones. |
| `productive_completed` | Runnable processors returned dicts with boolean `hasData` and no explicit `error`/`faceFailureStage`, apply plus the completion path returned, and applied metadata did not report failed/pending/running for executed steps. `hasData=false` without error is a legitimate detector no-hit result, not proof the photo lacks visible faces. This is an error-free applied-result proxy, **not** a durable verified unique-new-photo completion. |
| `completed_with_step_error` | Apply/completion path returned, but at least one runnable result explicitly reported an error or face failure stage. Can coexist with queue `done`; never counted as productive. |
| `completed_result_unknown` | Apply/completion path returned but result shapes did not satisfy the conservative productive predicate, without explicit error evidence. |
| `already_processed` | Fresh lease statuses plus the existing stale-face check left no runnable steps. Subset of queue `noop`. |
| `noop` | Also includes invalid/unsupported payloads or missing required dispatch fields; invalid non-object JSON now returns `noop` rather than claiming `done`. ACK behavior is unchanged. |
| `eligibility_reprocessing` | A terminal face step was explicitly re-enabled by the existing stale-embedding-version check. Counted when runnable work is selected, even if it later fails. |
| `eligibility_unknown` | Other runnable attempts. Pending/failed/running statuses do **not** establish new-upload eligibility. Counted before inference/apply, not just on successful completion. |

Eligibility and completion counters are overlapping dimensions, not a single
partition. Some reprocessing cannot be recognized with available evidence and
remains unknown. There is deliberately no `verified_new_completed` counter.
The apply return value is its mutable metadata snapshot updated with applied
statuses before persistence, not an independent post-write verification read.
Failed/pending/running applied states veto the productive proxy, while historical
`face_status=done` cannot override an executed face error. Results may also have been ignored by concurrent
write-time guards. Reprocessing, redelivery after failed ACK, duplicate messages,
partial-step work, later clustering failures and lease races prevent interpreting
the productive proxy as unique end-to-end completions. No new storage I/O is
introduced to resolve these uncertainties.

`productive_per_hour` and `done_per_hour` extrapolate the **window** counters
using actual window seconds. Zero-length final windows report a rate of zero.
ACK is independent: errors redeliver, lease contention below its retry limit
redelivers, and existing terminal outcomes (including retry exhaustion) are
deleted on the main thread. Historical `done`-based rebuild logic is retained
except that exhausted retries no longer inflate it.

## Fixed histograms and latency boundaries

`histograms.window` and `histograms.cumulative` report populated timing names.
Each throughput timing summary contains `count`, `sum`, `p50_upper_bound`,
`p95_upper_bound`, `p99_upper_bound`. Full `bucket_counts`/`bucket_upper_bounds`
are emitted as separate `ipwork latency histogram metrics=...` records, one per
fixed phase with window/cumulative data and replica identity. This avoids one
oversized all-phases console line. Retry depth's small arrays stay in throughput.
Arrays are noncumulative
bucket frequencies. The nearest-rank quantile selects the enclosing bucket's
**inclusive upper bound**, not interpolation or an exact percentile.

Latency bounds in milliseconds: 1, 5, 10, 25, 50, 100, 250, 500, 750, 1000,
2000, 5000, 10000, 30000, 60000, 120000, 300000, 600000, then overflow.
`null` as the final bucket bound means infinity; a null percentile means empty
or overflow, distinguishable using count and bucket frequencies. No observations
are silently clamped to the last finite bound. Retry-depth bounds: 0, 1, 2, 3,
5, 8, 16, 32, 64, then overflow. This is the observed **dequeue count**, not
dequeue count minus one; missing counts default to zero. Negative/nonfinite
observations are ignored.

| Timing | Boundary |
| --- | --- |
| `receive` / `receive_failed` | Entire receive call and lazy enumeration, including empty receives or failure. |
| `download` | Lazy source metadata read plus blob download, including failed attempts. Reused bytes generate no extra sample; failed downloads may be retried by later steps as before. Does not instrument storage-internal apply fallbacks. |
| `step_<step>` | Registered processor invocation only, including exceptions; download excluded. Fixed steps: preview, thumbnail, exif, ocr, face, ai_vision, map_detection. Missing processors/download failures do not imply an inference invocation. |
| `lease` | Processing-lease claim, including contention/not-found exceptions. |
| `steps` | Entire step runner, including download, preview swapping and its existing logging. |
| `apply` | Existing result application call, including exceptions and storage-internal work. |
| `cluster` | Existing best-effort clustering enqueue section; not downstream clustering completion. |
| `task` | Actual executor function start through processor return/raise, excluding main-thread ACK. Interrupted tasks never get fabricated completion samples. |
| `receipt_to_start` | End of successful receive enumeration to actual executor start. Includes batch preparation and waiting for a slot. Not queue insertion-to-start or upload-to-complete. |
| `ack` / `ack_failed` | Main-thread delete call, separated by outcome. |
| `receipt_to_ack` | End of receive enumeration through **successful** delete. Includes preparation, ready wait, task and main-thread wait. Failed or omitted ACKs do not enter this histogram. |
| `preparation` | Actual batch preparation function invocation through return/raise; not proof the preparation succeeded. |
| `wave` | Nonempty receive start through main-thread drain of held preparation/ready/future state and ACK attempts. Includes failed/deferred messages, so **not** a successful-wave metric. Interrupted waves are omitted. In batch-size-one rollback mode this is a continuous busy period, which can span refills, not a per-receive batch. |

Whole observations are attributed to the window in which they end; long tasks
can cross a window boundary. Histogram counts across different phases need not
match queue outcomes in the same window. Partial lazy receive failure retains
the existing behavior: partial messages are not submitted or ACKed.

## Loop state and wall-clock slot integration

`loop` reports active executing tasks, preparation message count (while the
preparation future is held), preparation age, ready messages and oldest submitted/unconsumed
task age. Oldest task includes a completed future awaiting main-thread handling;
it is not necessarily native inference age. `in_flight` still counts futures
held by the queue loop, which may differ from actively executing task count.

`utilization.window` and `.cumulative` include:

- `slot_seconds`: integral of actual active executor task count over monotonic
  wall time, updated at starts/finishes and snapshots.
- `slot_utilization`: slot-seconds / (configured concurrency × elapsed seconds).
  This is pipeline slot occupancy, **not CPU utilization**; I/O wait counts.
  Undefined for zero elapsed time (`null`).
- `preparing_seconds`, `ready_seconds`: wall time with corresponding held state.
- `preparation_only_seconds`: preparation held while no task is actively running.
- `idle_seconds`: no active task, held preparation or ready work. Includes queue
  receive/ACK, error sleep and bookkeeping when those states are empty; this is
  pipeline inactivity, not proof of an empty remote queue or idle process CPU.

Integration survives window resets. Phase wall times may overlap; they are not
an additive partition of total elapsed time. Preparation state ends when the
main thread consumes its future, rather than immediately on preparation return.

`defer_shutdown`, `defer_preparation_failed`, `defer_visibility_budget` count
unstarted messages deferred by each existing branch. `watchdog_preparation`
counts timed-out preparations and `watchdog_tasks` timed-out tasks at recycle;
`shutdown_grace_exhausted` counts grace exhaustion events. No receipt is ACKed
by instrumentation, a preparation failure, or a watchdog. Timeout/visibility
budgets and safe process-recycle behavior are unchanged.

## Resource measurements and the 10,000/hour target

`resources` reports current/peak process RSS in **bytes**, absolute process CPU
seconds, window/cumulative CPU deltas and CPU-cores consumed (CPU seconds /
wall seconds). CPU covers the entire process including sweeps/background work,
not individual threads; it can exceed one core. Baseline starts with worker-loop
instrumentation after prewarm. Peak RSS includes startup and is lifetime peak.

Linux current RSS uses resident pages × OS page size from `/proc/self/statm`;
Linux `ru_maxrss` is KiB × 1024. macOS `ru_maxrss` is already bytes; current
RSS uses Mach `task_info(MACH_TASK_BASIC_INFO)` resident bytes via standard-library
`ctypes` (no subprocess or polling). Unavailable RSS is `null`.
The legacy peak-memory log also now uses the correct platform conversion.
RSS is process-wide, not per thread/container/cgroup. No cgroup throttling
measurement or claim of actual CPU/RAM allocation is made.

For a separately confirmed **2 CPU, 4 GiB, concurrency 2** replica, 10,000
genuinely new photos/hour requires about 2.778 unique completions/second, or
166.67 per 60 seconds. At full slot occupancy, mean task service time must
be at most 0.72 seconds; preparation gaps, queue/ACK overhead and retries make
the available budget smaller. A two-core CPU budget similarly allows at most
0.72 CPU seconds/photo before other process work. Tail estimates, preparation
time, slot utilization, CPU-cores and RSS distinguish likely constraints but
do not prove causality or achievement of that target.

These tests validate measurement contracts using clocks/fake queues, not
production throughput or model accuracy. Verifying genuinely new completions
requires durable upload-generation identity, eligibility evidence, terminal
required-step persistence and cross-replica deduplication outside this
instrumentation. A productive proxy at 10k/hour alone is insufficient.

## Local validation (2026-10-04)

Configured Python environment; live-storage settings cleared for test processes.
Full backend suite: **1,486 passed**, 8 existing short-JWT-test-key warnings.
Retry-ceiling assertions now expect `retry_exhausted`, with unchanged terminal ACK.
`git diff --check` passed. Native macOS current/peak RSS smoke test passed;
Linux units and unavailable-resource handling use mocked tests, not a container
performance benchmark. No commit or deployment performed.

## Face-stage diagnostics

Fixed aggregate `face_decode`, `face_detect`, `face_landmarkWait`, `face_landmark`,
`face_align`, `face_embed` histograms use per-photo stage totals; landmark wait
is separated from landmark execution. Fixed counters distinguish
`face_no_detection`, `face_embedded`, `face_postprocessing_failed`, and
`face_quality_rejected`. Backend rejection diagnostics override the source
processor's apparent success. Counters describe attempts, not unique photos.

Persisted `client_face.faceDiagnostics` contains bounded counts, allowlisted
reason frequencies, timings, decoded dimensions, model readiness, output shapes,
maximum detector score, effective detector threshold and detector input size.
Detected-but-unembedded candidates and partial results are failed, not no-data;
incomplete passes cannot force-delete absent prior faces. Valid accepted partial
embeddings still follow canonical assignment without loosening merge thresholds.
See [the live audit](face-outcome-audit-20261004.md) for counts and model limitations.