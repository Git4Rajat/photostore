# Upload wave implementation sequence — 2026-10-23

This records follow-on work after the integration review. A passing primitive or
shadow-path test does not qualify the end-to-end production cutover.

| Order | Work | Current implementation status |
| --- | --- | --- |
| 1 | Whole-selection parent before 900-file transport children | Implemented in opt-in shadow upload path; reload, stop, retry, overlapping selections, duplicate canonical names and storage failure handled. Production admission authority, reservation deadlines and checkpoint retention still require integration. |
| 2 | Source generation fencing and durable completion | Claimed browser/Vision revisions and attempt tokens checked before result application. Opt-in face/embedding writes use create-only or pinned-version CAS; lookups carry source/attempt provenance. Completion backlog delivers to a durable idempotent inbox, with exact receipt acknowledgement and reconcile-page retry. Versioned JPEG publication and immutable artifact receipts are now implemented in the opt-in path; see the artifact audit below. Fixed required-step/model generations, legacy qualification, generation-aware retention/cleanup and independently scheduled recovery remain. |
| 3 | Real stage consumers and renewable stage ownership | Outstanding; prepared intents are isolated from index maintenance recovery, but no runnable stage dispatch or consumer exists. |
| 4 | Per-face decision journal and manifest streaming | Outstanding; current runner has bound inputs/chunks and oracle comparisons, not a durable per-face decision log. |
| 5 | Actual projection obligations, receipts and wave completion | Outstanding; exact-coverage ledger and conditional completion exist, real publishers are not connected. |
| 6 | Independent bounded recovery and alerts | Outstanding; no independently scheduled recovery host is wired. Source relay and library sweeps are primitives, not unattended recovery. |
| 7 | Activity gating and measured five-minute sleep behavior | Outstanding; live residency proof and cloud rollout not attempted. |

## Implemented behavior

The full operation metadata is written separately from the existing active-child
checkpoint. Per-item resolution markers are written only after successful server
acknowledgement. Losing the in-memory queue or failing a resolution cannot close
intake early. A failed full-parent storage write suppresses admission of its
children; it cannot silently downgrade grouping to independent chunks.

A retry links its newly declared unfinished subset to the preceding parent,
leaving old closed items immutable and cached file keys unchanged. The sync layer
ensures the predecessor admission exists before sending a linked retry. Explicit
stop/discard settles queued child items; queued items lost with the browser remain
unresolved until the future reservation recovery policy accounts for them.

Source completion capture defaults off behind
`UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED`. Failed, timed-out, leased, isolated and
deleted sources do not produce successful completion intents. Captured older
source generations remain in a bounded backlog until exact acknowledgement;
capacity exhaustion rejects a result commit rather than evicting recovery proof.
This is a metadata completion intent, **not** proof of immutable face/lookup/blob
results. The barrier must not trust it as such. Existing unclaimed browser and
finalize paths remain compatible and do not gain production fencing by omission.

## Validation

377 backend processing/claim/upload integration tests passed:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_processing_completion.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_processing_claim_batch.py \
  backend/tests/test_processing_lease.py \
  backend/tests/test_ipwork_race_guard.py \
  backend/tests/test_ipwork_dispatch.py \
  backend/tests/test_apply_client_processing_recovery.py \
  backend/tests/test_upload_metadata.py \
  backend/tests/test_finalize_batch_browser_exif_e2e.py \
  backend/tests/test_ipwork_cluster_batching.py \
  backend/tests/test_dedup_indexes.py
```

The full backend suite has pre-existing failures documented in the integration
review; the full rerun is recorded below. No deployment, flag enablement,
or live cutover is part of these checks.

75 frontend tests and TypeScript checking passed, from `frontend/`:

```sh
npm test -- --run \
  src/services/uploadWaveOperations.test.ts \
  src/services/uploadWaveSync.test.ts \
  src/services/uploadWaveClient.test.ts \
  src/components/AppServicesProvider.upload.test.tsx \
  src/services/indexReadyPush.test.ts
npm run typecheck
```


239 protocol/assignment/publication tests also passed:

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

These selections overlap; do not add their counts together.

Full backend rerun: **3,516 passed, 24 failed, 14 skipped and 28 setup errors**.
The failed/error test identities exactly match the integration-review run:
19 lexical parity cases, two lexical rebuild concurrency cases, two SQL
rating/likes ordering cases, the light-build dedup expectation, and 28 live-FAISS
fixture setup errors. No new failing identities appeared. The suite remains red;
these are not counted as passing checks.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

`git diff --check` and the final frontend typecheck passed. No code was deployed
and live cutover remains disabled.


## Follow-on: attempt fencing and source completion delivery

A fresh claim, including an expired same-owner reclaim, allocates a processing
attempt token. Live renewal by the same owner preserves it. Browser reports and
Vision results echo the captured token; a changed token fails before result
application, and metadata CAS retries check it alongside source identity.
Heartbeat and release also carry the captured token: an expired heartbeat cannot
revive an attempt, and cleanup from an old attempt cannot release a newer
same-owner claim.

With source completion capture enabled, a result-scoped fence checks photo,
revision, attempt, lease owner and expiry at face/lookup publication boundaries.
Face rows preserve the ETag read before building their curation-preserving update.
Embedding rows read their artifact version before checking the source attempt.
Writes use create-only or conditional replace, and never retry against a newer
artifact ETag. Concurrent creates and user curation therefore cannot be blindly
overwritten by a delayed artifact write. Face, embedding and complete lookup rows
carry the same source photo, revision and processing attempt. Cleanup failure
leaves the filename lookup incomplete and recoverable after lease expiry.

This does **not** claim complete multi-table or blob fencing. Unclaimed legacy
writers remain compatible; source-owned snapshots and generation-specific blob
publication are still needed before live cutover. Destructive face reconciliation
is explicitly rejected in the opt-in fenced path before any mutation until its
person/membership/delete writes have equivalent protection. Default legacy
behavior is unchanged. Do not enable the source-completion flag as a production
migration switch.

Terminal result commits now attempt delivery of one captured intent to the wave
store's source-completion inbox. Acceptance is content-addressed, library-scoped,
create-only/idempotent, and independent of whether upload admission has arrived.
The source acknowledges only an exact accepted inbox record matching completion,
library, photo and revision. Missing/wrong receipts, an outage, a crash after inbox
commit and lost replies retain replayable source evidence. Reconciliation pages
retry delivery without waiting for another browser report. The inbox records
`completionQualified=false`; delivery does not manufacture admission or stage
readiness. Intent payloads use schema version 2; persisted prototype version 1
backlogs, if any were created manually, require explicit migration rather than
being silently treated as current receipts.

Focused and broader validation commands/results for this follow-on are recorded
below; previous counts above refer to the earlier implementation checkpoint.

309 focused source/face/delivery and worker integration tests passed:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_processing_fence.py \
  backend/tests/test_processing_completion.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_face_by_filename_lookup.py \
  backend/tests/test_face_embeddings_table.py \
  backend/tests/test_upload_wave_shadow.py \
  backend/tests/test_processing_claim_batch.py \
  backend/tests/test_processing_lease.py \
  backend/tests/test_ipwork_dispatch.py \
  backend/tests/test_ipwork_race_guard.py
```

364 broader upload/execution/publication tests passed:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_upload_preparation_routes.py \
  backend/tests/test_apply_client_processing_recovery.py \
  backend/tests/test_ipwork_race_guard.py \
  backend/tests/test_ipwork_dispatch.py \
  backend/tests/test_upload_metadata.py \
  backend/tests/test_finalize_batch_browser_exif_e2e.py \
  backend/tests/test_upload_waves.py \
  backend/tests/test_upload_wave_execution.py \
  backend/tests/test_wave_publication.py \
  backend/tests/test_wave_clustering_parity.py
```

The same frontend selection recorded above still has 75 passing tests, and
`npm --prefix frontend run typecheck` passes. Selections overlap and their
counts must not be summed. The final full-backend rerun is recorded below.

Final full-backend rerun for this follow-on: **3,547 passed, 24 failed, 14
skipped and 28 setup errors**. The 52 failed/error identities exactly match the
prior run, with no additions or removals. The full suite remains red for those
baseline issues.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

`git diff --check` passes. No deployment or live cutover was attempted. At that checkpoint, step 2 still needed versioned blob publication and
qualified artifact receipts. The follow-on below records their implementation;
generation/model contracts and lifecycle/recovery integration remain open.
Steps 3–7 retain their checklist status above.


## Follow-on: versioned derivatives and immutable artifact receipts

See [the artifact receipt audit](upload-artifact-receipts-2026-10-23.md) for the
publisher, serving/index compatibility, qualified snapshot receipt integration,
no-model retry behavior, validation commands and remaining release gates.
Artifact-qualified inbox evidence still cannot release a wave. Item 2 remains
open for the generation/model contract and lifecycle/recovery integration;
items 3–7 retain their checklist status above.
