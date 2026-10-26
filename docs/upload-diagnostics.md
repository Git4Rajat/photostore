# Upload request diagnostics

The upload service emits one `upload timings` JSON record per request for
`/upload/init`, `/upload/init-batch`, `/upload/finalize`,
`/upload/finalize-batch`, and `/upload/client-processing` (including their
`/api` and trailing-slash aliases). Early validation/authentication/cleanup
returns, caught failures, and propagated exceptions are covered. These records
replace the previous success-only batch and client-processing timing messages.

## Fields

- `correlation_id`: fresh server-generated 32-character UUID hex; never taken
  from caller headers or upload IDs. Local log correlation only; no protocol or
  response-header change.
- `route`: canonical code-owned route, without query parameters.
- `status`: actual returned HTTP status after Flask error handling; 500 fallback
  for a propagated non-HTTP exception with no response.
- `outcome`: `success`, `http_error` (including caught 503s), `partial` (HTTP 200
  batch containing failed entries), `degraded` (successful response with caught
  phase exceptions), or `exception` (exception escaped the route).
- `total_ms`: elapsed monotonic server time through response construction.
- `phase_ms`: aggregate milliseconds for measured stages. Missing keys mean
  that stage was not reached/measured, not necessarily zero cost.
- `phase_errors`: aggregate exception counts by measured stage. A size/hash
  mismatch can return an HTTP error without throwing a phase exception.
- `counts`: applicable file, successful/failed batch-entry, and claimed-step
  counts; capped at 1,000,000. Claimed steps are counted, never logged by name.
- `finalized_bytes` and `finalized_files`: source bytes and files successfully
  finalized in an accepted response. Bytes come from the existing blob-size
  verification, are not capped by the file-counter limit, and are zero for
  init/client-processing calls and HTTP errors. Partial batches credit only
  their successful entries.

Phase names and counter keys are allowlisted. Batch durations sum across files;
there is no new per-file timing log. No filename, hash, user ID, credential,
SAS URL, request header, image payload, exception text, or client result enters
these records. Existing application/storage logs are unchanged and may contain
their historical fields.

## Coverage and interpretation

Initialization covers authentication/library guard and JSON parsing; cleanup,
tracking/reservations, and image/thumbnail SAS creation are separately measured.
Finalize measures pending-name resolution, blob property verification,
`finalize_metadata`, metadata stamping/reads, optional inline client processing,
and aggregate processing/activity/clustering enqueues. Storage contributes
dedup, final-name resolution, metadata reads, and metadata/index persistence;
client processing includes status-batch flush and lease-reset persistence.
`source_read` measures eager finalize downloads when needed.

Storage phases nest inside route phases: **do not sum all phase values to
calculate total request time**. Timings include synchronous waits/retries in
the measured calls, not background completion. Local validation, metadata
assembly, name mapping, optional video fallback, and response serialization can
also contribute to the total without a dedicated subphase. Internally swallowed
storage failures cannot be inferred from the outer call's exception counter.

The browser's hashing, processing, block staging/commit, direct image/thumbnail
blob transfers, browser/network delays, request queueing before route execution,
and later ipworker/clustering execution are **not measured by server request
timers**. Use browser/network telemetry and existing worker diagnostics for
those boundaries. No upload protocol or performance optimization is introduced.

Instrumentation is request-local and best-effort: clock/logging failures do not
change response bodies, statuses, or application exception propagation. A phase
context records elapsed time even on exceptions and re-raises the original error.

## Upload volume throughput (MB/hour/replica)

The upload role also emits `upload throughput metrics=<JSON>` every 60 seconds
of monotonic wall time, including idle windows, and a final shorter window on
graceful Gunicorn worker exit/recycle. A single daemon thread per worker samples
fixed thread-safe counters; it makes no storage calls and retains no photo,
user, request or upload IDs. Startup and shutdown use Gunicorn's
[worker lifecycle hooks](https://gunicorn.org/reference/settings/#post-worker-init).

`window` and `cumulative` include `requests`, `request_errors`, `finalized_files`
and `finalized_bytes`. Requests cover the instrumented routes above, not health
probes or every upload API. `finalized_mb_per_hour` is
`window.finalized_bytes / 1000000 * 3600 / window_seconds`, matching vision's
decimal MB and wall-time denominator. Zero-length final windows report zero.
Only accepted finalize responses credit bytes. Hash/size mismatches, missing
blobs and failed files do not count; successful files in partial batches do.
Caught enqueue/stamping failures can still produce accepted finalization and
byte credit; this metric does not imply downstream processing completed.

The current upload deployment uses one Gunicorn process per replica, so this is
MB/hour/replica. Each sample includes allowlisted replica identity, process ID,
a fresh process-instance identifier, and actual `workers_per_replica`. With
multiple processes the replica rate is `null`; `process_mb_per_hour` remains
available. Combine process volume and replica uptime before reporting a replica
rate in that configuration. Do not divide bytes by summed concurrent request
durations or average rounded sample rates.

This measures **source volume finalized by the upload app**. Image transfer
goes browser-to-Blob and does not pass through this app, so this is not measured
network bandwidth or upload-session duration. Finalize retries can credit the
same file again: these are successful finalization attempts, not verified unique
library growth. No durable deduplication or extra storage I/O is added.

The following LAW query puts upload finalization alongside productive vision
processing, weighting each pipeline's total bytes by its own recorded
replica-hours. It supports the current one-process upload configuration:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(6h)
| where ContainerAppName_s in ('forenkladev-upload', 'forenkladev-vision')
| where Log_s has 'upload throughput metrics=' or Log_s has 'ipwork throughput metrics='
| parse Log_s with * 'throughput metrics=' metrics_json
| extend m = parse_json(metrics_json)
| extend pipeline = iff(ContainerAppName_s == 'forenkladev-upload', 'uploads', 'vision')
| where pipeline != 'uploads' or toint(m.workers_per_replica) == 1
| extend source_bytes = iff(pipeline == 'uploads',
    todouble(m.window.finalized_bytes), todouble(m.window.productive_source_bytes))
| where isnotnull(source_bytes)
| summarize source_bytes = sum(source_bytes),
    replica_seconds = sum(todouble(m.window_seconds)) by pipeline
| extend mb_per_hour_per_replica = iff(replica_seconds > 0,
    source_bytes / 1000000.0 * 3600.0 / replica_seconds, real(null))
```

Process restarts reset cumulative totals; aggregate `window` counters only.
Cold initialization before the hook, missing logs and abrupt termination without
a final sample leave coverage gaps. Logger failure can drop a sample; reporting
is best-effort and must not block uploads. Windows are selected by log emission
time and may cross the requested period boundary. Upload and vision are separate
stages, often handling the same source bytes; their volumes must not be added
and called distinct uploaded data.

### Wave clustering cost attribution

`run_cluster_release` emits one `cluster_wave_summary` and bounded
`cluster_wave_phase` totals per attempt, including failed attempts. Correlate
using `wave_id`, opaque `library_id`, `process_instance`, and replica identity.
The summary reports elapsed `milliseconds`, `process_cpu_ms`, `faces`,
`faces_processed`, outcome, and scope storage counters when available. A replay
of an already completed stage can report success with zero processed faces.

Phases include lease acquisition, frozen snapshot preparation, runtime build,
matching, curation validation, durable journal writes, assignment persistence,
local runtime updates, checkpoints, and publication handoff. `guard_check` and
`eligibility_check` measure repeated control checks; `guard_policy_read`,
`guard_wave_read`, `guard_dispatch_read`, `guard_ownership_read`, and
`guard_keepalive_check` separate their causes. Each phase reports invocation
count, error count, elapsed time, calling-thread CPU, and scope storage deltas.
No per-face phase log lines or additional storage calls are introduced.

Phase timings are **inclusive**: guard time inside persistence is included in
both totals. Do not add nested phase totals together. CPU and network time can
overlap. Process CPU includes native worker threads and other work in the same
process; phase thread CPU excludes native helper threads. Storage deltas carry
`sdk_attribution=shared_scope`, since concurrent work sharing a performance
scope can contribute to them. This label also applies to Vision's existing
`processing_apply_phase` events. Missing storage counters mean no performance
scope was available, rather than zero storage activity.

### Bounded Vision inspection failures

An empty serialized completion outbox (`"[]"`) is treated as having no relayable
intent. It does not establish successful processing: normal receipt qualification
or the fenced source adapter must still provide completion evidence.

Inspection exceptions occur before source-attempt admission. Pending members now
persist a separate `inspectionFailures` counter (maximum three) and
`inspectionError` exception type. At exhaustion, a matching source without a live
lease is settled as `failed_exhausted` with reason
`inspection_failures_exhausted` and a durable settlement witness. This counts as
a failure, never successful inference, and allows other members to reach the
clustering barrier. Completed members and active source owners are protected;
ownership loss or failure to persist inspection evidence propagates to stage
retry handling. The existing one-attempt model-processing limit is unchanged.

A Vision continuation containing unresolved inspection errors and no advance in
`lastProgressAt` retains its dispatch-attempt charge. Healthy waiting and passes
that advance member progress retain their existing continuation behavior. Five
failed dispatch attempts remain the stage limit; exhaustion requires operator
retry rather than an indefinite loop. Updating inspection counters alone does
not count as member progress.

### Derivative receipts after replacement uploads

Upload finalization clears `thumbnailArtifact` and `previewArtifact` when it
allocates a new processing revision. Explicit empty values clear these columns
through both conditional replacement and legacy merge persistence. A retry with
the same physical photo, content hash, and existing revision retains its receipts.
Historical completion outboxes and immutable derivative blobs remain intact.

Prepared-derivative promotion skips a receipt only after validating its current
photo/revision binding and structure. Stale or malformed receipts are replaced
by reading the current photo's browser-prepared bytes, validating them, and
publishing immutable artifacts under the active source fence. Old receipt IDs
are never relabeled as proof of new content. This does not automatically
reopen members already settled as failed in a frozen wave.

### File-backed wave assignment journals

New clustering stages use `PEOPLE_FAISS_CHECKPOINT_DIR/wave-journals` when the
checkpoint mount is configured. Snapshot and decision records are append-only
JSONL on the trusted mount, with a SQLite lookup index on local work disk only.
The complete snapshot is flushed before a compact, execution-fenced `local-v1`
Table marker is published. Every subsequent decision is flushed before its
conditional person/member/face writes. A compact completion receipt remains in
Table Storage. Existing table-backed manifests continue on their original path;
without a configured checkpoint directory the table journal is still used.

The mount must provide cross-process flock and fsync, matching the existing
clustering checkpoint contract. A marked journal that disappears or becomes
corrupt fails closed; restore the mounted journal before retrying. Rebuilding
from a partially materialized live baseline is not safe. A torn final append is
truncated, while an interrupted unpublished snapshot can be captured again.
Journal files are retained for recovery; this change does not add automatic
retention/deletion of completed wave journals.

A background heartbeat renews durable execution ownership. The clustering loop
checks local lease validity on every guard call, refreshes durable eligibility
at least every second when checks are invoked, and forces a refresh at each
assignment-page boundary. Lease expiry or heartbeat failure aborts the attempt;
conditional entity ETags and library-lease checks remain on materialization.
This bounds the observation delay for policy/release changes to the refresh
interval between boundaries, rather than rereading all control rows per face.
Wave transitions and compact journal markers retain transaction fencing.

Vision receipt capture may reuse the exact derivative receipt whose immutable
bytes were published and read back in the same source-fence context. Only two
small receipt fingerprints are retained, no image bytes. Changed receipts and
new/reentered source contexts require fresh verification; ordinary verification
continues to read storage. `derivative_promotion` and `artifact_capture` spans
now expose these previously unmeasured apply costs.

The synthetic benchmark `backend/scripts/benchmark_wave_assignment_files.py`
compares 1,000 faces with real matching and fake Table clients. It verifies
output parity and reports client-call counts. Its local-disk timings contain no
Azure latency and must not be interpreted as production throughput.

### Vision and publication dependencies

`waiting_predecessor` waits for the previous wave's Vision evidence to freeze,
verified by its frozen revision and cluster outbox (or exact no-work completion
receipt). A previous wave stuck in clustering or indexing does not block Vision.
The Vision consumer wakes its direct successor at this boundary; recovery also
promotes existing waiting waves, including waves admitted before this change.

After Vision, a successor remains `ready` until its predecessor has exact
publication-completion coverage. Cluster dispatch and worker claims enforce this
gate, as do assignment commits and empty/zero-face fast paths. This preserves
clustering/publication ordering while allowing later Vision waves to drain.

### Invalid derivative pointers during indexing

The October 26 indexing failure was one error chain: a stale cross-photo
thumbnail receipt caused `Invalid derivative pointer` in the sort projection;
the builder returned `None`, which became `Required publisher produced no
artifact`. Sort and access projections now omit invalid derivative receipts
without dropping the photo. Thumbnail URLs fall back to the current photo's
physical identity. Valid receipts from a previous revision of the same photo
remain usable while processing continues. Projection does not rewrite metadata,
repair bytes, or qualify failed source processing as successful.

Required streamed sort/access builds propagate original scan, data-upload and
manifest-upload errors. They cannot produce wave publication receipts after a
failed build. The fallback error for a publisher returning no output identifies
the scope. A previously exhausted wave still needs a fresh dispatch revision
after the corrected image is deployed; deploying code alone does not immediately
reset its attempt budget.
