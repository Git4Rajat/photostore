# Shadow upload-wave foundation — 23 October 2026

Implemented locally, disabled by default. No deployment, production queue
release, new clustering trigger or change to browser upload behavior.

## What is implemented

`backend/upload_waves.py` owns transactional protocol records in a separate
`upload-wave-shadow-<library hash>` partition of the existing jobs table. These
records do not appear in the library's ordinary job-status partition or the
index-maintenance pending partition. No extra table or infrastructure is needed
for this local shadow foundation.

Every admission, registration, resolution, close and processing observation
conditionally updates the library control row in the same Azure Table
transaction as its affected records. There are no wildcard ETags or unconditional
upserts. Retry attempts are bounded. A parent session declares an immutable
selection count before registering its items in pages of at most 20. The parent
can span all 900-file transport chunks; a 901-item test cannot close after only
900 items resolve. Stable session/item IDs make retries idempotent.

Accepted items bind to a photo/revision pair, with duplicate references sharing
one member. Cancelled/failed transfer items resolve intake without fabricating
accepted processing work. Closure requires all declared items to be registered
and resolved, the current manifest revision, and a stable close request ID.
Closed sessions cannot admit more items; a resumed intake needs a new session.

Overlapping sessions join the collecting wave. Admission before freeze reopens
intake, invalidates the old evaluation and changes the intake epoch. The final
close and a non-dispatchable shadow Vision marker commit atomically. Readiness
scans session/member evidence in bounded RowKey ranges, persisting progress.
Each page conditionally checks the captured control revision; concurrent
admission or observations restart evaluation. Cached counts must agree with the
manifest. Freeze and its non-dispatchable shadow cluster marker commit together.
Later admissions enter a successor with an explicit predecessor dependency.

Shadow successors intentionally stay `waiting_predecessor`: this slice has no
qualified indexing-completion receipts or successor-promotion implementation.
It cannot manufacture predecessor completion from current legacy job statuses.

Status exposes wave age, state age, barrier duration, time since useful progress,
last evaluation time, outstanding members/leases, failures and predecessor wait
reason. Evaluation exposes `scanInProgress`, `scannedMembers` and
`scannedSessions`. Status reads and unchanged observations do not reset stall
timers. These are inspection fields, not deployed monitoring alerts.

## Source observations are not production receipts

`backend/upload_wave_shadow.py` reads persisted metadata and the complete face
filename lookup. Unfinished/retryable steps, missing or inconsistent lookup
records, absent face rows, invalid embeddings, source replacement and uncleared
leases remain pending. Lease expiry alone does not settle a member. A successful
zero-face result requires an explicit complete empty lookup. Storage failures
propagate; a missing source row is not treated as a deletion tombstone. Reads
are bounded to 20 members per reconciliation call, with up to 128 faces per
member; larger face sets remain pending in this diagnostic adapter.

All API responses identify `protocolMode=shadow`, `dispatchable=false` and
`evidenceQualified=false`. Legacy source rows do not yet have the target wave
processing-generation/result-commit fence. The adapter's physical-photo/hash
identity only supports observations of fresh-upload data; it cannot distinguish
all reprocessing generations. Metadata, face rows and lookup reads are also not
one cross-table snapshot. Source changes after observation can make an
observation stale without a source outbox notification. Consequently, **shadow
readiness must never release a real stage or satisfy production completion**.

## Explicit opt-in API

Enable only in a development/shadow environment with
`UPLOAD_WAVE_SHADOW_ENABLED=true`. It is absent from deployment defaults.
Normal upload/init/finalize, client-processing and legacy release paths are
unchanged. This backend API is not yet invoked by the browser.

All paths below are under `/upload/waves/shadow`, require authenticated library
context and use that library rather than a body-supplied library ID. Requests
are limited to 64 KiB and registration pages to 20 items.

| Method/path | Request / behavior |
| --- | --- |
| `POST /sessions` | `{sessionId, declaredCount}`; declare the whole parent selection |
| `POST /sessions/{session}/items` | `{items: [{itemId, filename}]}`; register intended items |
| `POST /sessions/{session}/resolve` | `{itemId, outcome, filename?}`; `accepted`/`duplicate` bind from server metadata at the finalized filename; `cancelled`/`failed` resolve unaccepted items |
| `POST /sessions/{session}/close` | `{manifestRevision, closeId}` from the latest registration/resolution response; never asserts processing completion |
| `POST /{wave}/reconcile` | `{after?}`; observe one member page from authoritative sources; repeat using `nextCursor` until empty |
| `POST /{wave}/evaluate` | `{}`; advance one readiness page; repeat while `scanInProgress`; blocked intake/predecessors do not advance |
| `GET /{wave}` | No-store inspection of shadow state/progress |

Disabled routes return 404; invalid payloads return 400, protocol conflicts 409,
and storage unavailability 503. Processing outcomes or `complete` fields in a
client reconciliation/evaluation request are not trusted.

## Validation

The new protocol/API/source suites passed **87 tests**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_upload_waves.py \
  backend/tests/test_upload_wave_shadow.py
```

They exercise real transaction boundaries against an ETag-enforcing atomic fake:
selection/chunk boundaries, overlaps, deduplication, stale closure, concurrent
admission/freeze, observation races, resumable multi-page evaluation, storage
failure before commit, retry exhaustion, lease blocking, no-op markers,
predecessor waiting, tenant isolation, source validation and client spoofing.
They do not establish Azure throughput or production source fencing.

The final regression selection passed **440 tests**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_upload_waves.py \
  backend/tests/test_upload_wave_shadow.py \
  backend/tests/test_upload_init_batch.py \
  backend/tests/test_upload_diagnostics.py \
  backend/tests/test_upload_face_creation_boundary.py \
  backend/tests/test_apply_client_processing_recovery.py \
  backend/tests/test_processing_lease.py \
  backend/tests/test_index_ownership.py \
  backend/tests/test_index_maintenance_worker.py
```

These selections overlap; their counts are not unique coverage.

The full backend command completed with **3,413 passed, 24 failed, 14 skipped
and 28 setup errors**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

Its failing/error test identities exactly match the preceding index-boundary
run and the original-code reproductions documented in
[index-boundary implementation validation](index-boundary-implementation-2026-10-23.md).
The full suite remains non-green. `git diff --check` passes.

## Next implementation gates

1. Wire persistent parent/item IDs into browser selection, queued 900-file
   chunks, retries, stop and resume. Register the full selection before its
   first transfer; do not use telemetry `uploadBatchId` as authority.
2. Allocate processing generations in authoritative source writers and persist
   completion/pending evidence with their fenced commits. Reconcile lost
   source-to-protocol notifications; do not promote this observational adapter
   into a production receipt mechanism.
3. Add durable reservation recovery/abandonment and a bounded independent scan
   execution host. Quiet time must not close unresolved transfers.
4. Run actual uploads through shadow comparison, including queued selections,
   zero-face/duplicate files, browser/worker results, source races and crashes.
5. Implement qualified predecessor publication receipts, promotion, renewable
   stage claims and fenced Vision/cluster/index handoff relays before cutover.

Sequential residency, one production clustering job per wave, generation-fenced
reprocessing and automatic completion after browser interruption are not claimed
by this slice.
