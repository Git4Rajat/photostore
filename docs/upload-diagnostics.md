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
