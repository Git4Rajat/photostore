# Refresh / Discard / re-upload admission contention

## Evidence

The supplied `download.devtools` HAR contains 448 requests. A restored
7,249-item parent replayed registration in four requests taking roughly
11–21 seconds each despite all items already being registered. Its background
outcome cleanup continued from 13:42:42 through 13:53:43 UTC. The new parent's
registration received 16 HTTP 409 responses between 13:44:57 and 13:47:55 UTC,
each reporting `Concurrent wave mutation; retry the request`. Old cleanup
requests overlap those new registration requests. The last old resolution
batch also received `Finalized photo not found` twice. Replaying reconstructed
browser outcomes could therefore leave intake open after losing its checkpoint.

The HAR is not copied into the repository; it contains sensitive request data.

## Fix

- Discard retains its checkpoint and blocks Retry/new upload until server
  closure is acknowledged. Failure preserves recovery information for retry.
- Live owner Discard uses the existing paged abandonment endpoint. It refreshes
  the manifest revision through idempotent session open and stops restored
  registration after its in-flight page. Server-resolved uploads are preserved
  without replaying stale browser outcomes. Finalization revision races refresh
  with a bounded retry. Library members retain the ordinary resolve/close path
  if owner abandonment is forbidden; actual closure is required for success.
- Abandonment commits cancellation, counters and cursor atomically for each
  bounded 20-item page. Live finalize leases refuse the whole page. ETag and
  policy fences remain enforced. Final closure uses the normal domain handoff.
- Registration uses bounded queries for existing items and reads the global
  control fence immediately before committing, shortening its conflict window.
- Mutation exhaustion has a distinct retryable code. The browser retries only
  that transient 409, at most three times with jitter and identical payloads.
  Authenticated scope is checked before replay; permanent conflicts fail at once.
- Stale run callbacks cannot erase Discard's checkpoint or a replacement upload.
  Artifact cleanup precedes server closure, before processing is released.

## Validation

467 backend tests across 24 wave/admission/table-read modules passed. Frontend
service/provider tests cover acknowledged discard, failure/retry, restoration,
member fallback, finalize races, late callbacks, bounded contention retries and
authentication-scope changes. TypeScript validation passed.

This is a local source change. It does not deploy images, repair live sessions,
add session expiry/heartbeats, or introduce a standing recovery worker.
