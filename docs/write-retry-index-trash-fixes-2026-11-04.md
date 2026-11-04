# Write, retry, index, and trash correctness fixes

This batch addresses audit findings F1–F5 and the related delete/restore concurrency finding F7 in [the flow audit](audits/dead-code-flow-2026-11-04.md). It does not retire legacy UI or change shared-library favorite ownership.

## Saved edits and acknowledgements

Single rating, like, and rotation endpoints now check the metadata writer's result. They return `503` with `code=metadata_write_failed` when no successful commit is confirmed. Bulk rating returns only successfully saved filenames in `rated`, with per-file errors for the remainder. The active frontend confirms only those edits and rolls back the others.

The metadata writer now separates committing the source from delivering its index refresh. Failed projection delivery retries separately without replaying the source mutation. After a successful source commit, an index delivery failure cannot turn that save into a false source-write failure. Durable mode retains its existing source-bound publication intent for recovery; legacy delivery gets bounded retries.

Likes accept an optional boolean `liked` target. The current frontend sends the desired state, so explicitly retrying after a lost reply does not undo an already-applied favorite. Conditional-write retries recompute membership from the fresh row while holding that target fixed. An empty body retains the legacy toggle contract for older clients.

## HTTP retry policy

The common HTTP client automatically retries only GETs after network/gateway failures. Missing replies and `503` responses do not prove a write never executed. Arbitrary POST/PUT/DELETE requests therefore make one transport attempt. User-facing connection errors no longer promise continued automatic retries.

The upload protocol's existing explicit retries and persisted operation identities remain its own retry authority. This change removes the additional shared-transport retry layer beneath them. Session-scope checks and the explicit single-attempt option remain covered by regression tests.

## Index delivery and cache consistency

The manifest summary now includes its delta sequence, and the sort-index route supplies the matching delta URLs. The frontend applies all required deltas after a fresh base download, or only new deltas when it has a matching cached base. Incomplete manifests and mismatched delta lineages/sequences are rejected rather than treated as current data; the existing server-paged fallback remains available.

The delta sequence participates in the persisted projection revision. This preserves upload-generation/edit reconciliation and prevents an older tab's delayed write from replacing a newer cached sequence. An already-stored partial snapshot without the new revision evidence is downloaded again.

The building-status poller uses the indexer client for `/api/tools/indexes/status`, matching backend role registration.

## Trash and concurrent edits

Soft delete and restore use ETag-conditional updates and bounded conflict retries, preserving unrelated concurrent edits. They cannot recreate a source purged between read and write. Missing rows are distinguished from storage outages.

A failed trash-index add now propagates failure to the request/job instead of silently acknowledging completion. Delete replay repairs that secondary write even when the metadata is already deleted, preserving the original deletion timestamp and pre-delete processing statuses. Background delete jobs retain their payload on failure for queue retry. A synchronous failure requires explicit retry; this batch does not rebuild historical missing trash entries automatically.

## Validation

Results on the completed changes:

- 544 backend tests passed across 18 selected suites, including metadata concurrency, trash, sort indexes, inline publication, upload preparation, and wave recovery/ownership paths.
- All 912 frontend tests passed across 63 files.
- TypeScript checking passed. ESLint reported zero errors on the changed frontend files; existing warnings remain.
- `git diff --check` passed.

The frontend run used the installed Node runtime's `--no-experimental-webstorage` option so jsdom supplies browser storage. Without it, Node's experimental `localStorage` shadows jsdom's implementation and causes unrelated storage tests to fail. No application/test-environment configuration was changed for this workaround.

```bash
cd frontend
NODE_OPTIONS=--no-experimental-webstorage ./node_modules/.bin/vitest run --maxWorkers=4
./node_modules/.bin/tsc --noEmit
```

Regression coverage includes failed read/write acknowledgements, partial bulk saves, desired-state and legacy likes, conditional-write conflicts, projection delivery after source commit, lost mutation responses, read retries and session changes, fresh/cached sort deltas, replacement upload generations, cross-tab cache ordering, trash-index failure/replay, and delete/restore racing metadata edits or purge.

The original audit probes intentionally assert the baseline defects and are retained as historical reproductions. The normal regression suites assert the corrected behavior.
