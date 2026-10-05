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