# Upload-wave integration review and hardening — 23 October 2026

Follow-on implementation status and validation are recorded in
[the implementation sequence audit](upload-wave-sequence-2026-10-23.md).
The remaining-work list below reflects this review checkpoint.


Reviewed the available history through `63c5e31` on top of checkpoint `6669a7d`.
The clustering runner, receipt ledger and visibility gate are useful additions,
but their presence does not complete the live migration: no stage dispatcher,
Vision wave consumer or catalog publisher connects them end to end.

## Correctness changes implemented

1. **Publication capture survives interruption and conflicting creators.** A
   create-once manifest commits the entire required set before obligation-row
   materialization. Completion reads that set by exact ID, not whatever rows
   happened to survive a partial write. Retrying restores missing rows. A
   competing different set is rejected. Explicit empty capture proves the
   no-obligation case; no manifest means unknown/incomplete. Manifest size is
   checked before writes.
2. **Receipts carry evidence and dependencies.** A receipt needs either an
   output generation or an explicit no-change witness. Exact prerequisite IDs
   participate in obligation identity and must have receipts before dependent
   publication is acknowledged. Conflicting duplicate receipts are rejected.
   Real publisher ownership, stage fencing and active-artifact verification
   still belong to the future publisher integration.
3. **Successor release waits for indexing coverage.** `complete_indexing`
   validates a library/wave/frozen-input-bound completion proof against the
   wave's captured publication frontier. It commits `complete` and the coverage
   record atomically. Promotion verifies this record and the predecessor's
   frozen revision. A predecessor at `ready`, or a bare `complete` flag without
   coverage, remains a blocker. There is not yet a live stage owner that binds
   the publication frontier/enters indexing/calls completion.
4. **Wave preparation is isolated from index recovery.** Reusing `WorkStore`
   in its default partition would expose `wave-vision:...` scopes to the running
   indexer's generic recovery loop, despite omitting an explicit `notify` call.
   `stage_work_store` now uses `upload-wave-stage-pending`, and preparation
   rejects a default index store or mismatched library/destination. Reopened
   intake creates a distinct epoch-specific intent. Conditional outbox marking
   rejects a release that became ineligible during intent preparation. A
   stranded prepared intent cannot be recovered by the catalog index worker.
5. **Configuration cannot strand faces before consumers exist.** The legacy
   trigger remains active while `live_consumers_available()` is false. Neither
   execution nor per-library environment flags can perform a live cutover on
   their own. The future implementation must replace this guard with validated
   consumer/cohort ownership; merely changing the constant is insufficient.
6. **Clustering retries bind the complete input.** The journal captures library,
   full canonical input digest (including source vectors), face count and chunk
   size before assignment. Changed vectors, added tail faces, changed chunk
   layout and mismatched face/photo membership fail before assignment. Journal
   updates now explicitly use conditional ETags. This remains an input/chunk
   journal, not a record of individual matching decisions, and does not yet
   provide stage-owner fencing or streamed large-manifest execution.
7. **Browser diagnostics conform to the server protocol.** Actual persisted
   file keys contain `:` and filenames; the server's identifier validation
   rejects those keys. The wire client now hashes the complete key into a
   stable 64-character hex ID for both registration and resolution, preserving
   checkpoint data. A per-session promise chain orders registration, resolution
   and close, so early completion and reordered responses cannot supply a stale
   close revision. Failures remain diagnostic-only. A client build flag defaults
   diagnostics off, avoiding per-file 404 traffic when server shadow mode is off.

The historical outbox field `dispatchable` records intent preparation only in
this prototype. No runnable stage queue send or worker claim is implemented.
Stage consumers must revalidate eligibility at claim and renewed ownership at
writes; generic work leases alone cannot supply these guarantees.

## Validation

237 focused backend tests passed:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_clustering_parity.py \
  backend/tests/test_wave_publication.py \
  backend/tests/test_upload_wave_execution.py \
  backend/tests/test_upload_waves.py \
  backend/tests/test_index_work.py \
  backend/tests/test_clustering_incremental_assign.py \
  backend/tests/test_index_maintenance_worker.py \
  backend/tests/test_index_publication.py \
  backend/tests/test_upload_wave_shadow.py
```

66 frontend tests and TypeScript checking passed, from `frontend/`:

```sh
npm test -- --run \
  src/services/uploadWaveClient.test.ts \
  src/services/uploadWaveSync.test.ts \
  src/components/AppServicesProvider.upload.test.tsx \
  src/services/indexReadyPush.test.ts
npm run typecheck
```

The full backend run finished with **3,497 passed, 24 failed, 14 skipped and 28
setup errors**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

The failing/error test identities exactly match the previously recorded baseline
(lexical parity/async rebuilds, SQL ordering, light-build dedup expectation, and
live-FAISS setup errors). No new failing identities were introduced. The full
suite is not green. These test selections overlap; do not sum their counts.
`git diff --check` passes. No deployment or live cutover was attempted.

## Remaining live-path work, in dependency order

1. Persist a parent admission for the entire selection/queued chain before the
   first 900-file chunk starts. Map each child session to it across reload,
   retry, stop and resume. The present browser hook tracks individual persisted
   sessions and is not sufficient for one-shot grouping of a large selection.
2. Allocate/fence source processing generations for browser and Vision work.
   Persist completion/pending outboxes with authoritative result commits and
   reconcile lost notifications. A revision field alone does not qualify
   metadata/face/lookup observations as atomic source completion.
3. Implement durable frozen manifests, stage-slot ownership, renewed claims and
   eligible relays/consumers for Vision, cluster and indexing. Complete the
   per-face decision journal and broader oracle/restart qualification before
   driving the real matcher through wave jobs.
4. Connect index publishers to exact obligations and output-generation receipts,
   keeping foreground edits isolated from held wave work. Bind the frontier
   before publication; complete/promote only from its durable coverage.
5. Implement independent bounded recovery, reservation reconciliation, poison
   handling and operator metrics/alerts, including an execution host that can
   run while all application replicas are zero.
6. Replace the temporary cutover guard with a durable per-library migration
   protocol; drain/reconcile legacy buffers and prevent mixed ownership.
7. Finish foreground/inactivity gating beyond tab visibility, five-minute
   cooldown configuration, and measured inactive/active-user replica timelines.

The new modules are not evidence that the upload -> Vision -> one clustering
wave -> indexer residency sequence is already running in production.
