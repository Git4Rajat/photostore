# New-library wave ownership, settlement and cleanup — 2026-10-24

This supersedes the first three activation-gate bullets in the
[publication/recovery audit](upload-wave-publication-recovery-residency-2026-10-23.md).
It implements the requested remaining items 1–4. Historical library migration is
excluded: users will move data into newly created libraries. Cloud qualification
(item 6) remains required. No deployment, capability certificate, live enrollment
or production flag change was performed.

## Durable ownership and upload admission

`wave_policy.py` enrolls only genuinely new, empty libraries through
`LibraryStore.before_create`. A `library-wave-v1` policy is permanent ownership,
independent of the execution flag. Pausing execution cannot restore automatic
legacy assignment/index/source-release work. Existing libraries are not converted.
Queued legacy automatic work is acknowledged without running its old consumer;
explicit foreground album/people edits retain incremental indexing.

Enrollment requires backend processing, source completion and generation fencing,
and a content-addressed capability certificate listing qualified deployment IDs
for all eight roles: core, upload, vision, cluster, index-worker, wave-recovery,
archive and recovery. Each live API/consumer verifies its deployment identity
against that certificate. `wave_policy.certify(table, deployments)` is an operator
primitive, not automatic qualification. These settings must be intentionally
provided only after cloud qualification:

- `PROCESSING_MODE=backend`
- `UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED=true`
- `PROCESSING_GENERATION_ENFORCEMENT_ENABLED=true`
- `UPLOAD_WAVE_CAPABILITY_ID=<qualified certificate ID>`
- `UPLOAD_WAVE_DEPLOYMENT_ID=<this role's certified immutable build ID>`
- `UPLOAD_WAVE_NEW_LIBRARIES_ENABLED=true` on library-creation hosts
- `UPLOAD_WAVE_EXECUTION_ENABLED=true` on participating hosts
- `UPLOAD_WAVE_RECOVERY_ENABLED=true` on the scheduled recovery host
- `VITE_UPLOAD_WAVE_EXECUTION_ENABLED=true` in the qualified frontend build

The deployment template still leaves live execution disabled. The scheduled
recovery host needs the same qualified configuration as consumers; its previously
added dormant template is not an enabled production recovery service.

The frontend discovers ownership through authenticated, noncached
`GET /upload/waves/protocol`. The response is cached only for that browser's
account/library scope because ownership never changes for an existing library.
A discovery failure fails live admission closed and is retryable. Legacy libraries
continue their existing upload path in the same frontend build.

Live `init`, `init-batch`, `finalize`, and `finalize-batch` require a registered
whole-selection parent, stable hashed item identity, filename and SHA-256 binding.
900-file transport children share the parent. Browser result/claim endpoints
cannot bypass Vision ownership. Source writes use conditional metadata updates
or create-only writes; failed live reservations cannot fall back to issuing SAS
for an unreserved plaintext filename. Live retries/resumes use the same direct
reservation path. A request lease and bounded, nonretrying metadata write protect
against finalization outliving owner-confirmed abandonment. Init and finalize
cannot overwrite a deletion tombstone or a concurrent metadata change.

Successful finalization durably resolves its reservation, including canonical
renamed filenames, before reporting success. Fully resolved intake closes on the
server, so losing the browser's close request cannot strand accepted sources.
A failed bind returns a retryable error. Closing the last live session seals the
wave before Vision release; later selections belong to a successor. Freezing a
predecessor cannot clear that successor's collecting pointer. A successor created
before its predecessor freezes binds the exact completed predecessor revision
and coverage receipt during conditional promotion.

## Blocked waves and explicit repair

`wave_settlement.py` records distinct, content-addressed exclusion/failure
witnesses. Missing rows alone never mean success. Automatic settlement requires
a deletion tombstone/witness, a durably admitted replacement generation, or five
actual source attempts with failed step statuses and no live source lease.
Completion can report `applied_with_failures`; failed sources never receive
successful processing receipts.

Forced full reprocessing atomically records a bounded supersession backlog with
the new generation. Recovery admits the replacement before excluding the old
one, then conditionally acknowledges the backlog. A crash between these actions
replays deterministic identities. Frozen input is retained through its owning
wave; a new Vision stage waits for predecessor publication.

Authenticated library owners can use:

- `POST /upload/waves/live/sessions/<session>/abandon` with the current
  `manifestRevision` and a nonempty `reason` (at most 256 characters). One call
  processes at most 20 reservations; use the returned revision while
  `abandonmentNeedsMore` is true. Live request leases defer abandonment. Accepted
  sources remain obligations; never-registered/reserved items are cancelled.
- `POST /upload/waves/live/<wave>/repair`, `action=retry-stage`, to request a new
  dispatch revision after the current stage lease ends.
- The same repair endpoint with `action=confirm-missing`, `photoId`,
  `processingRevision` and `reason`, only after authoritative metadata returns
  404. This is an explicit exclusion, not a zero-face receipt.
- `action=retry-source` with the exact member identity for full generation-based
  reprocessing. Frozen obligations cannot be rewritten by these repair calls.

The independent scanner still emits state age, barrier wait, lease/progress and
pending-handoff measurements. Cleanup-paused libraries do not dispatch stage
work. Poison repair is deliberate; recovery never manufactures successful work.

## No-work advancement

`wave_no_work.py` performs bounded control-only scans. All-cancelled intake and
all-duplicate waves already covered by a completed publication can finish with
`outcome=no_work`, a proof witness and successor promotion, without waking source,
FAISS or index builders. Accepted data promotes `needsProjection` monotonically,
so a duplicate binding cannot erase a later accepted obligation.

Qualified zero-face/excluded input skips Cluster artifact production while
retaining the real index obligations. The control fast path takes the same
Cluster execution lease as a delayed worker message, and cannot replace an
applied assignment journal. No-op receipts remain distinct from applied receipts.

## Destructive cleanup and publication races

Permanent photo deletion, library clean and purge now call the wave-aware cleanup
integration. Permanent deletion first pauses wave mutations using the existing
expiring policy GC lease, refuses live source leases/frozen input, conditionally
tombstones metadata and records deletion witnesses/explicit pre-freeze member
settlements. This prevents readiness freezing between the deletion survey and
its tombstone. Physical cascades begin only after that preparation succeeds.
Frozen-member deletion is deliberately deferred until publication finishes;
full library clean cancels unfinished waves under a durable cleanup fence after
stage leases drain. Open upload intake must first be stopped or abandoned.

Cleanup references include live metadata receipts/outboxes, source inboxes and
unfinished-wave members. Shared immutable snapshots/blobs survive while any
retained manifest references them. Old inbox references release only after their
source is gone and no unfinished wave depends on it. Completion admission is
conditional on the same policy row as the GC pause, closing the reference-survey
race. Known deleted-attempt staging, orphan snapshots, immutable projection
history and manifests are cleaned with durable bounded cursors/deletes. Corrupt
reference evidence blocks deletion. A failed deletion or purge does not report
successful cleanup. GC failures after a successful photo cascade leave a durable
retry request rather than changing the photo deletion result.

Artifact cleanup uses a separate durable pending partition and the existing
library-operations queue. The scheduled scanner only relays this request; it does
not perform the library-wide reference survey itself. Crashes, lost queue sends
and incomplete bounded passes remain retryable. The collector currently surveys
all relevant metadata/references in memory before bounded physical deletion;
large-library memory/runtime qualification is still required. It does not infer
unknown abandoned staging attempts from blob-name absence; future staging writers
must preserve exact attempt identities or provide a separately qualified orphan
inventory policy.

Wave publication and foreground CRUD builders share a renewable library catalog
lease using the existing Blob lease implementation in a separate namespace from
FAISS. This covers read/build/publish, preventing an earlier foreground snapshot
from overwriting a later wave publication. Blob wrappers retain the lease guard
when publishers cross thread boundaries and fail closed after ownership loss.

## Validation

Validation commands and results are recorded below.
The backend and frontend have pre-existing failing tests; focused green suites
are not a claim that their full suites are green or that cloud behavior is proven.

**526 focused backend tests passed** (the selections overlap with older audits;
do not add their counts):

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=backend:backend/tests \
  .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_activation.py backend/tests/test_wave_live_routes.py \
  backend/tests/test_wave_finalize_fence.py backend/tests/test_wave_cleanup_live.py \
  backend/tests/test_wave_execution_consumers.py backend/tests/test_upload_waves.py \
  backend/tests/test_upload_wave_execution.py backend/tests/test_wave_catalog.py \
  backend/tests/test_wave_runtime.py backend/tests/test_wave_recovery.py \
  backend/tests/test_wave_publication.py backend/tests/test_library_utils.py \
  backend/tests/test_processing_cleanup.py backend/tests/test_generation_cleanup_acceptance.py \
  backend/tests/test_clustering_lease.py backend/tests/test_index_publication.py \
  backend/tests/test_admin_repair_async.py backend/tests/test_library_download.py \
  backend/tests/test_clustering_incremental_assign.py backend/tests/test_ipwork_deferred_release.py \
  backend/tests/test_upload_metadata.py backend/tests/test_upload_preparation_routes.py \
  backend/tests/test_upload_init_batch.py backend/tests/test_finalize_batch_browser_exif_e2e.py
```

Three existing fake Table clients now raise the real SDK's
`ResourceNotFoundError` for absent rows. RuntimeTable preserves MERGE semantics.
The former environment-list cutover test now proves durable ownership suppresses
legacy work even while execution is paused. No production error handling was
weakened to accommodate these fakes.

**83 focused frontend tests passed**, and TypeScript passed:

```sh
npm --prefix frontend test -- --run src/services/uploadWaveClient.test.ts \
  src/services/uploadWaveOperations.test.ts src/services/uploadWaveSync.test.ts \
  src/services/foregroundDemand.test.ts src/services/indexReadyPush.test.ts \
  src/components/AppServicesProvider.upload.test.tsx
npm --prefix frontend run typecheck
```

Full frontend: **693 passed, eight failed**, the same eight failure identities
already reproduced against committed HEAD in the previous audit (seven perf
localStorage fixtures and the album persisted-receipt expiry cleanup case).

```sh
npm --prefix frontend test -- --run
```

Full backend command:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=backend:backend/tests \
  .venv-photostore-tools/bin/python -m pytest -q backend/tests
git diff --check
```

Final full backend: **3,760 passed, 24 failed, 28 setup errors, 14 skipped**.
All 52 failed/error identities exactly match the historical step-4 baseline;
there are no new identities. The known background lexical-rebuild timing case
passed in the preceding run (3,759 passed / 23 failed / 28 errors before the last
two init-race tests were added) and failed in this final run; it remains unfixed.
The strengthened pre-freeze successor test also passed independently:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_activation.py
# 13 passed
```

`git diff --check` passes. The next work is item 6: a controlled cloud library
qualification proving real SDK/queue failure recovery, representative throughput
and memory bounds, IAM/alert/recovery execution, overlapping upload and foreground
CRUD behavior, and all-eight-app residency measurements. Local results do not
satisfy that release gate.
