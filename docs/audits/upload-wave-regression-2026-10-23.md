# Upload and wave-design regression review — 23 October 2026

## Result

491 selected tests passed: 350 backend and 141 frontend. No failing regression
was observed in those suites. Four implementation/design gaps must be closed
before claiming the proposed inactive-user sequence is implemented:
upload -> Vision -> cluster -> indexer -> zero.

This was a review of the current upload path and target design, not a completed
wave implementation review. The wave protocol is still a document. Existing
tests exercise current behavior, including legacy scheduling; green results do
not prove the target pipeline or app scale-down behavior.

Scope: checkout HEAD `55d881f`, current upload/preparation, processing-release,
index scheduling and notification code, deployment template, and
`docs/indexing-clustering-target-state.md`. Existing unrelated album/prototype
edits were left untouched. No live Azure requests, queue consumption, production
uploads, deployments or application implementation edits were performed.

## 1. P1 — background job polling wakes the indexer before its stage

`frontend/src/components/AppServicesProvider.tsx:1531` starts a poll loop and
continues while jobs remain active, at the 6-second `JOB_POLL_INTERVAL_MS`.
`frontend/src/services/jobNotifications.ts:102` sends these reads to the Tools
API on indexer. Processing jobs therefore keep a passive tab polling indexer
while Vision is working. The loop has a maximum lifetime, but it can still
violate the five-minute idle goal during that lifetime. An open visible tab is
not evidence that a user is actively browsing.

Required change: define inactivity explicitly and stop routine job/index/status
polls when inactive. Obtain catch-up status on real foreground interaction;
notifications can use the optional push transport when configured. Refocusing
must catch up without replaying stale completion or dropping pending edits.
Do not remove the correctness reconciliation merely to achieve scale-down.

Acceptance: an idle visible tab and a hidden tab generate no repeated Tools/core
poll requests during Vision/cluster; returning to the tab resolves completed
jobs and pending edits. Verify actual request/scaler timelines.

## 2. P1 — upload completion couples Vision release to premature indexing and cluster work

`backend/routes/upload.py:421` schedules `dedup` at upload end and line 422
requests `ipwork_release`. Finalization also schedules `dedup-idle` and a
release check through `_queue_idle_hash_refresh` at line 426. Release/check jobs
are processed on cluster's library-ops queue. In dedicated mode the hash job
wakes indexer; in legacy mode catalog work is itself cluster work. Neither
route provides the intended upload -> Vision-only handoff.

The frontend already avoids its normal completed-end hash signal while queued
batches remain (`AppServicesProvider.tsx:3935`). That is useful and must be
preserved, but it is not a server-side parent manifest: quiet checks, other
sessions and recovery still need the durable admission protocol.

Required change: close intake and persist/release Vision work independently of
hash publication and cluster-hosted release checks. Store hash/catalog dirty
intents for the final indexing stage. Preserve upload dedup correctness while
the published hash snapshot is intentionally older; authoritative finalization
checks must remain effective for duplicates introduced in the same upload.

Acceptance: all three 900-file chunks and overlapping sessions finalize before
Vision starts; neither cluster nor indexer is awakened for upload-end work.
Duplicate content within one selection and across overlapping selections is
still handled correctly without requiring a freshly rebuilt hash snapshot.

## 3. P1 — scope-only index work cannot establish wave coverage or isolate foreground work

`backend/index_work.py:87` identifies work by library and scope. Current
publication notifications identify library/scope/job rather than an immutable
wave input plus a complete set of assignment/publication revision receipts.
That is useful coalesced maintenance but not proof that one wave is covered.

In addition, a foreground People/album edit may wake the indexer during Vision.
If a consumer simply drains all shared dirty rows in that scope, it can run the
background publication that the wave design intended to defer. Calling the job
"incremental" does not by itself restrict which rows or source revisions it
consumes.

Required change: distinguish eligibility from dirty state, retain wave/revision
coverage independently of library/scope coalescing, and define exact foreground
publication eligibility. A foreground operation can publish its required patch
against a valid baseline without releasing unrelated deferred wave work.
Publication of incidental source data must never be mistaken for completion of
the wave's required dependency set. If existing whole-snapshot publishers cannot
provide that isolation, implement it or explicitly narrow the claimed contract
before cutover.

Acceptance: edit an existing album/person while wave face results are only
partially committed; the edit becomes visible, deferred wave work stays gated,
and old/unrelated job receipts cannot mark the wave complete. A later final
publication covers the assignment revision without losing the foreground edit.

## 4. P1 — waking the indexer API also starts its maintenance sidecar

The proposed dedicated deployment co-locates Tools HTTP and the index-maintenance
consumer in one indexer app (`deploy/resources.bicep:1003` onwards), scaled by
both HTTP and queue rules at line 1030. A foreground Tools/status/CRUD request
can therefore start a consumer even when its background wave stage is gated.
`run_index_worker` also runs periodic ledger recovery while alive.

Required change: enforce stage eligibility in consumer claims, ledger recovery,
read-trigger hooks and legacy dispatch, not only in the scaler or initial queue
send. The correct behavior when awakened early is to service eligible foreground
work while leaving deferred wave intents unsent/unclaimed. Recovery must apply
the same policy. Keep independent operator recovery available without making
indexer a permanent coordinator.

Acceptance: force an early indexer replica start during upload/Vision/cluster,
with pending deferred intents and duplicate legacy wakeups; no background wave
publication runs. Explicit eligible foreground edits still complete.

## Passed checks and limitations

Backend command:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_upload_init_batch.py \
  backend/tests/test_upload_preparation_routes.py \
  backend/tests/test_upload_metadata.py \
  backend/tests/test_upload_face_creation_boundary.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_ipwork_deferred_release.py \
  backend/tests/test_index_maintenance_worker.py \
  backend/tests/test_index_update_scopes.py \
  backend/tests/test_ipwork_cluster_batching.py -q
```

Result: 350 passed in 3.54 seconds (pytest reported runtime).

Frontend command, from `frontend`:

```sh
npm test -- \
  src/services/uploadPreparationPipeline.test.ts \
  src/services/uploadAssetPreparation.test.ts \
  src/services/uploadSessionStore.test.ts \
  src/services/uploadKnownHashes.test.ts \
  src/utils/uploadDerivatives.test.ts \
  src/utils/uploadMetadata.test.ts \
  src/components/AppServicesProvider.upload.test.tsx
```

Result: seven files, 141 tests passed; Vitest duration 10.80 seconds.

Coverage includes preparation/fallback, direct-asset finalization order,
metadata packet validation, upload persistence, processing-release retry and
legacy buffering, metadata concurrency and existing index scopes/maintenance.
No new executable reproductions were added. Findings above are source-backed
migration gaps, not newly failing tests or an exhaustive security audit.

Two precision notes from the review:

- The normal direct original upload has a periodic `/upload/transfer-heartbeat`
  request while staging blocks (`AppServicesProvider.tsx:3026`). Direct byte
  transfer does not imply that the upload app currently sleeps during a long
  transfer; the target explicitly permits upload to be awake during intake.
- A Blob metadata packet and extracted EXIF JSON are different paths. Packet
  bytes can go directly to Blob; normalized browser EXIF and preparation
  receipts pass through finalization. Preserve validation and fallback before
  releasing Vision; do not declare supported EXIF complete solely from a client
  upload receipt.

Before release, implement the target-specific acceptance cases, test crash and
rolling-deployment interleavings, and collect real app request/replica/queue
timelines. This local pass cannot certify five-minute scale-down or production
stage isolation.
