# Fixed processing generations and cleanup integration — 23 October 2026

Follow-on to the [artifact receipt audit](upload-artifact-receipts-2026-10-23.md)
and the [implementation sequence](upload-wave-sequence-2026-10-23.md), item 2
("fixed processing generations") and the start of item 4 ("cleanup
integration"). No deployment, flag enablement, or live stage cutover is
included. `UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED` and
`UPLOAD_WAVE_EXECUTION_ENABLED` remain off by default and unchanged by this
work.

**Status: item 2 is partial.** A code review against the first pass found
five correctness gaps before any real stage consumer should be wired to
these contracts. All five are fixed below, with new tests reproducing each
failure mode; the review's own 18 original cleanup/supersession/acceptance
tests did not exercise them. Item 2 stays marked partial because the
browser/Vision wire protocol still carries no generation/contract ID (gate
5) and retry-exhaustion settlement is still open (gate 1) -- see remaining
gates.

## Fixes from code review

1. **Forced reprocessing was not flag-gated and could strand unrelated
   steps.** `app._enqueue_processing_steps(force=True)` called
   `supersede_processing_generation` unconditionally, which resets the
   WHOLE required-step set to pending -- but the function only re-queues
   the caller's named subset afterward. Requesting `force=True` for just
   `['face']` reset all seven steps to pending while only re-queuing
   `face`, leaving the other six at `pending` with no queued work behind
   them, and did so even with live cutover disabled. Fixed: a new
   `PROCESSING_GENERATION_ENFORCEMENT_ENABLED` flag (default off) gates
   supersession entirely -- disabled, `force=True` behaves exactly as it
   did before `processing_generation.py` existed. Enabled, supersession
   only fires when the caller's `steps` cover the *entire* required-step
   set for that media type; a partial selection falls through to the old
   behavior (re-queue only the named steps, generation untouched). See
   `processing_generation.enforcement_enabled()`.
2. **The model contract was incomplete, and nothing independently detected
   a deployment-wide model bump.** `build_contract()` omitted the photo
   (CLIP/OpenCLIP) embedding version and a Vision taxonomy version; added
   as `photoEmbeddingVersion` (mirrors `vision_utils`'s env-configured
   model identity without importing torch/open_clip into this leaf module)
   and `visionTaxonomyVersion` (a new, currently-advisory
   `VISION_MODEL_TAXONOMY_VERSION` env var -- nothing cross-checks a
   Vision result's own taxonomy against it yet; see gate 5). Separately,
   the original commit-time fence only compared the row's generation
   between `baseline` and `current` -- it could not catch a model bump
   that happened *before* a claim even started, because both reads would
   already show the same stale generation. `_commit_worker_processing_
   results` now also calls `processing_generation.is_superseded(current,
   is_video=...)` independently, gated by the same enforcement flag and
   only when the row already carries a generation (so legacy/unmigrated
   rows are unaffected). The original acceptance test for "a model
   upgrade rejects the old result" only mutated the row directly; a new
   test bumps only the live env var, with no row mutation, and shows the
   independent check still rejects the in-flight commit.
3. **Cleanup could delete a derivative blob shared with a retained
   manifest.** A published derivative blob is content-addressed by its
   bytes, not by which manifest captured it -- a recapture under a
   different generation can produce byte-identical output and reference
   the *same* blob name. `delete_orphaned_manifest` deleted every blob an
   orphaned manifest referenced with no check for this. Fixed: new
   `protected_artifacts(retained_manifest_rows)` aggregates every blob/
   snapshot a set of confirmed-*retained* manifests still reference;
   `delete_orphaned_manifest` now accepts `protected_blobs`/
   `protected_snapshots` and skips (not deletes) anything in them, same
   face-snapshot caveat as before but now actually enforced for blobs and
   made explicit for snapshots too.
4. **The reference survey could misclassify a retained manifest as
   orphaned.** `referenced_manifest_ids` treated `processingCompletionAck`
   (a completion-*intent* identity, a different identity space from an
   artifact manifest ID) as a manifest reference -- a no-op bug today
   (it never collided with a real ID) but a conceptual error asserting a
   reference that doesn't exist. It also silently swallowed malformed
   JSON in a row's completion evidence, which would *undercount*
   references and could make a still-needed manifest look orphaned, and
   it never surveyed the durable source-completion inbox
   (`upload_waves.ShadowWaves`'s `source-completion!...` rows), which can
   hold a manifest reference independent of the row's own current state.
   Fixed: the bogus `processingCompletionAck` line is removed; any
   unparseable completion evidence now raises instead of being skipped
   (corrupt/incomplete evidence blocks deletion, per the review's own
   framing); and `referenced_manifest_ids` takes an optional `inbox_rows`
   iterable, scanning each entry's durable payload for its own
   `artifactReceipt` reference.
5. **Video completion still contradicted its own contract.**
   `processing_generation.required_steps_for_media(True)` is `()`, but
   `prepare_completion` unconditionally required all seven statuses to be
   in `TERMINAL` -- and finalize's video overrides leave `thumbnail_status`/
   `exif_status` at `'failed'`, which is not a `TERMINAL` value. Every
   finalized video was therefore permanently ineligible for completion.
   Fixed: `prepare_completion` now derives the row's actual required-step
   set via `is_video_file`/`required_steps_for_media` and only requires
   *those* steps to be terminal; the intent payload gained a
   `requiredSteps` field, and `step_provenance` gained a `required`
   parameter returning `'not_required'` for any step outside that set
   regardless of its status value. `validate_intent` independently
   re-derives the required set from the intent's own `filename` and
   rejects a payload whose `requiredSteps`/`stepProvenance` disagree with
   it (schema bumped to version 5).

## Implemented

1. **A real processing generation, content-addressed and bound to source
   identity.** `backend/processing_generation.py` freezes, per photo:
   photo identity, source-byte hash, the required-step set for that media
   type (image gets the full seven-step set; video gets none -- it is
   browser-owned and immediately settled at finalize), and the live
   face-embedding/photo-embedding/Vision-taxonomy/policy versions, into
   one SHA-256 contract ID. A new generation is allocated only alongside a
   newly allocated `processingRevision` -- never on an ordinary lease
   renewal, heartbeat, or retry, which retain the existing generation and
   only mint a fresh attempt token (`processingFence`).
2. **Wired into the three places revisions are allocated or consumed.**
   `storage_utils.finalize_uploaded_file` allocates the generation alongside
   a freshly allocated revision (new upload or detected source change).
   `storage_utils.claim_processing_lease` defensively backfills a missing
   generation for pre-migration rows (mirroring `processingRevision`'s own
   `setdefault`), without ever overwriting one that already exists, and now
   returns `processingGeneration` alongside `processingRevision`/
   `processingFence` in its response. `_commit_worker_processing_results`'s
   existing source-changed fence compares `processingGeneration` between
   baseline and current, and (gated, see fix 2 above) independently checks
   the current row against the live contract.
3. **Forced reprocessing is a real, flag-gated, full-contract supersession;
   ordinary retry and a partial forced selection are provably unaffected.**
   `storage_utils.supersede_processing_generation` allocates a fresh
   revision/generation bound to the row's current photo/source and resets
   every required step for its media type to pending, clearing the
   in-flight lease/attempt/artifact-receipt pointer. `app.py`'s
   `_enqueue_processing_steps` calls it only when
   `PROCESSING_GENERATION_ENFORCEMENT_ENABLED=true` AND the caller's
   `force=True` request names every required step (see fix 1).
4. **The result fence and artifact publisher are generation-aware.**
   `processing_fence.ResultFence` gained an optional `generation` field
   (backward-compatible default `''`, so existing positional construction
   is unaffected); when present, `check()` and `headers()` include it, so a
   published derivative's pointer and every write through
   `processing_artifacts.publish`/`write_artifact` carry the generation that
   authorized them.
5. **The completion intent binds the generation, the row's actual
   required-step set, and distinguishes terminal provenance.**
   `processing_completion`'s schema is version 5: the intent carries the
   64-hex-char `generation` id, `requiredSteps`, and a `stepProvenance` map
   (`verified` / `no_data` / `policy_excluded` / `not_required` per step,
   cross-checked against `statuses` and the media type's actual required
   set at validation time -- see fix 5). `prepare_completion` requires a
   recorded generation to consider a row completion-eligible at all --
   missing provenance cannot be inferred as "no work needed." There is
   deliberately no `retry_exhausted` provenance yet: a *required* step
   stuck at `failed` is still outside `TERMINAL` and still blocks
   completion indefinitely -- that settlement path remains open (gate 1).
6. **An explicit, named outcome for a wave member whose generation moved on.**
   `backend/wave_supersession.py` is a standalone primitive:
   `member_outcome(member, row, is_video=...)` returns `'valid'` (the row
   still matches what the wave captured), `'missing'` (hard-deleted, nothing
   to settle with), or `'superseded'` (a forced reprocess or a live
   model/policy bump moved the row's generation past what this wave froze).
   It never mutates the frozen member and never silently adopts the new
   generation's identity. No real wave-completion evaluator calls it yet --
   see gate 4.
7. **Cleanup primitives for the new namespaces, not yet wired into live
   deletion.** `backend/processing_cleanup.py`: `delete_staging_attempt`
   (refuses to delete the row's currently-active attempt token),
   `referenced_manifest_ids` (aggregates every manifest ID live metadata
   rows and the durable completion inbox still point to -- see fix 4),
   `orphaned_manifest_candidates`/`retained_manifest_rows` (a read-only
   partition scan split by referenced-ID membership), `protected_artifacts`
   (aggregates blobs/snapshots a retained set still needs -- see fix 3),
   and `delete_orphaned_manifest` (deletes a confirmed-orphaned manifest's
   published blobs, then its face snapshots, then the manifest row itself,
   skipping anything in the protected sets -- blobs first, so an
   interrupted deletion leaves the manifest row as evidence of what is
   still outstanding rather than orphaning blobs with no remaining record).

## Remaining release gates

1. **Retry-exhaustion settlement.** A *required* step stuck at `failed`
   still blocks `prepare_completion` indefinitely; there is no explicit
   exhausted-retry/forced-partial-completion outcome yet, separate from
   `policy_excluded`/`no_data`/`not_required`.
2. **Live wiring into permanent deletion.** `processing_cleanup.py`'s
   functions are not called from `app.py`'s `_hard_delete_photos_chunk`,
   `_execute_library_clean`, or `_purge_library_data`. Doing so safely still
   needs: fencing active writers during cleanup (so a late worker holding an
   old lease cannot recreate data after deletion -- the acceptance test only
   proves the *existing* fence already rejects a late publish once
   `processing_state` flips to `deleted`; nothing yet *drives* that flip
   together with artifact/manifest cleanup in one coordinated sequence), and
   a real survey step that calls `referenced_manifest_ids` across every live
   row AND every inbox row in a library before trusting
   `orphaned_manifest_candidates`'/`retained_manifest_rows`' output.
3. **No real wave-completion evaluator.** `wave_supersession.member_outcome`
   is unused outside its own tests; nothing calls it during an actual
   evaluation of a frozen wave's members. Step 3 (real stage consumers) and
   step 6 (projection obligations/receipts) are still the modules that would
   need to call it.
4. **Browser/Vision wire protocol unchanged.** `claim_processing_lease`'s
   response carries `processingGeneration` now, but nothing sends it back
   from the browser or Vision on result submission, and no result-commit
   caller requires it in `expected_processing_fence`/`expected_processing_
   revision`'s kwarg style yet. Enforcement today is entirely server-side
   and flag-gated (`PROCESSING_GENERATION_ENFORCEMENT_ENABLED`); a client
   that silently reuses a stale contract is caught at commit time once that
   flag is on, not by a protocol-level rejection before work is even done.
   `visionTaxonomyVersion` specifically is advisory only -- introduced as a
   contract field with no existing Vision-side value to cross-check it
   against yet.
5. **Legacy duplicate/retained-face migration.** `is_superseded` correctly
   reports `True` for any row with no recorded generation, but there is no
   migration/backfill job that qualifies or explicitly re-derives generations
   for an existing library's historical rows -- see the artifact-receipts
   audit's own gate 2, still open.
6. **`PROCESSING_GENERATION_ENFORCEMENT_ENABLED` itself needs a rollout
   plan.** It currently gates two independent behaviors (full-contract
   forced-reprocess supersession, and the commit-time deployment-staleness
   check) with one flag. Splitting them, or sequencing when each is safe to
   enable per environment, hasn't been decided.

Item 3 of the delivery sequence (real stage consumers, durable release,
renewable ownership) is next, per the user's explicit sequencing; live
cutover stays disabled until those consumers can themselves enforce the
generation/contract checks above, not just the standalone primitives.

## Validation

New/updated focused suites, run together:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_processing_generation.py \
  backend/tests/test_processing_generation_integration.py \
  backend/tests/test_wave_supersession.py \
  backend/tests/test_processing_cleanup.py \
  backend/tests/test_generation_cleanup_acceptance.py \
  backend/tests/test_processing_completion.py \
  backend/tests/test_processing_fence.py \
  backend/tests/test_processing_artifacts.py \
  backend/tests/test_processing_receipts.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_upload_wave_shadow.py \
  backend/tests/test_clustering_incremental_assign.py \
  backend/tests/test_app_role_blueprint_registration.py
```

262 passed. Acceptance scenarios from item 5 of the request, and where each
lives (updated/added rows marked):

| Scenario | Covered by |
| --- | --- |
| A model upgrade during processing rejects the old result (row-level) | `test_metadata_write_concurrency.py::test_worker_conflict_preserves_completed_step_and_source_replacement[...processingGeneration]` |
| A model upgrade **deployment-wide, no row mutation** rejects an in-flight result (new) | `test_metadata_write_concurrency.py::test_deployment_model_bump_rejects_an_in_flight_result_with_no_row_mutation` / `::test_deployment_model_bump_is_inert_while_enforcement_is_disabled` |
| Forced reprocessing creates a new generation; ordinary retry does not; a partial forced selection never strands unrelated steps (updated) | `test_processing_generation_integration.py::test_forced_full_reprocess_allocates_a_new_generation_when_enforcement_is_on`, `::test_forced_partial_reprocess_never_resets_or_strands_unrelated_steps`, `::test_forced_full_reprocess_is_a_noop_while_enforcement_is_disabled`, `::test_ordinary_retry_never_allocates_a_new_generation_or_revision` |
| An older wave survives, or explicitly settles, supersession | `test_wave_supersession.py` (primitive only -- no live evaluator yet, see gate 3) |
| Legacy duplicates with missing provenance cannot falsely qualify | `test_generation_cleanup_acceptance.py::test_legacy_duplicate_with_missing_provenance_cannot_falsely_qualify` |
| Interrupted cleanup resumes safely | `test_generation_cleanup_acceptance.py::test_interrupted_cleanup_resumes_safely` |
| Cleanup preserves artifacts needed by another active wave, **including a blob/snapshot shared with a retained manifest** (updated) | `test_generation_cleanup_acceptance.py::test_cleanup_preserves_artifacts_needed_by_another_active_wave`, `test_processing_cleanup.py::test_delete_orphaned_manifest_never_deletes_a_blob_or_snapshot_shared_with_a_retained_manifest` |
| Corrupt/incomplete completion evidence blocks deletion instead of undercounting references (new) | `test_processing_cleanup.py::test_referenced_manifest_ids_raises_on_corrupt_evidence_instead_of_undercounting` |
| A finalized video is completion-eligible despite non-terminal thumbnail/exif statuses (new) | `test_processing_completion.py::test_a_finalized_video_is_completion_eligible_despite_failed_thumbnail_and_exif`, `::test_video_eligibility_is_unaffected_by_a_genuinely_stuck_required_step` |
| Late workers cannot republish deleted data | `test_generation_cleanup_acceptance.py::test_late_worker_cannot_republish_after_permanent_deletion` |

Full backend run: **3,624 passed, 24 failed, 14 skipped and 28 setup
errors**. The failed/error identities exactly match every prior audit's
baseline (19 lexical parity cases, two lexical rebuild concurrency cases,
two SQL rating/likes ordering cases, the light-build dedup expectation, and
28 live-FAISS fixture setup errors) with no additions or removals:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

`git diff --check` passes. No deployment, flag enablement, or live cutover
was attempted.

## Review corrections: generation and stage execution

The execution flag now gates first delivery, prepared-outbox delivery,
consumer claims and `recover_releases`. Recovery callers must use that
stage-specific wrapper instead of calling `WorkStore.recover` directly.
A current Vision release remains eligible in `processing`, so a pending
pass can resume. Reopened intake invalidates the old release before further
barrier mutations.

Consumers require `ExecutionLeases(store.table, partition=store.partition)`.
Wave mutations conditionally update the execution record's ETag in the same
transaction as the wave/control records; expired or replaced ownership
cannot advance a stage. Completion and failure reject expired leases even
without a replacement owner.

Vision readiness no longer uses the shadow observer. It requires the exact
source completion inbox record, immutable artifact manifest, current source
and model contract, and matching admitted required steps. An unresolved
source needs an explicit `process_source(library, filename, revision, renew)`
adapter using the source lease/result-fence path. There is no default legacy
worker fallback: it would independently trigger clustering. Missing adapters
fail explicitly; deleted, replaced, superseded or actively leased sources
remain blocked rather than being processed under an old obligation.

Cluster input loading now uses the exact completion IDs selected at readiness
and their immutable snapshots. It never substitutes live face rows or treats
missing lookups as zero faces. **Nonempty assignment remains explicitly
blocked before mutations:** the existing assigner reloads live rows and lacks
stage-fenced per-face commits. Step 4 must supply that path; this correction
is not a claim that real assignment execution is complete. Zero-face inputs
can produce a conditionally fenced index handoff. Real index publishers and
independent scheduled recovery remain unfinished.

Enforced partial forced reprocessing is rejected without changing old proof
or status. Full forced reprocessing supersedes; default legacy behavior is
unchanged. Video artifact capture and shadow admission use the media-specific
required-step set, consistent with source completion.

The frozen face-model allow-list includes both default deployed identities
(browser AdaFace and worker AdaFace), not only environment overrides. A
pending durable source outbox is relayed before asking the source adapter
to recover missing receipts.

Review-fix validation: **494 passed** in the affected backend selection:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_execution_lease.py \
  backend/tests/test_wave_execution_consumers.py \
  backend/tests/test_processing_*.py \
  backend/tests/test_generation_cleanup_acceptance.py \
  backend/tests/test_wave_supersession.py \
  backend/tests/test_upload_wave*.py \
  backend/tests/test_wave_publication.py \
  backend/tests/test_wave_clustering_parity.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_ipwork_dispatch.py \
  backend/tests/test_ipwork_race_guard.py \
  backend/tests/test_sort_index.py \
  backend/tests/test_photo_access_batch_deleted_fallback.py \
  backend/tests/test_proxy_thumbnail_preview_fallback.py \
  backend/tests/test_raw_native_preview.py
```

After the final native Table API compatibility and missing-adapter lease
cleanup edits, **35 passed** in the overlapping ownership/consumer selection:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_execution_lease.py \
  backend/tests/test_wave_execution_consumers.py
```

`git diff --check` passed. These are focused/broader affected tests, not a
new full-backend run. No flags enabled, deployment or commit performed.
