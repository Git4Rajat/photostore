# Versioned upload artifacts and receipts — 2026-10-23

Follow-on to the [implementation checklist](upload-wave-sequence-2026-10-23.md),
within item 2. No deployment, flag enablement or live stage cutover is included.

## Implemented

- Claimed browser derivative uploads use an attempt-specific staging path when
  `UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED=true`. That SAS never targets published
  output. Source photo/revision/attempt come from the server's claim record.
- Browser staging and server-generated JPEGs share one publisher. It checks the
  current attempt, enforces byte/pixel limits, conditionally reads staged bytes,
  hashes the verified content, and uses create-only publication in a separate
  namespace. Existing output is verified on retry. A final source check precedes
  adding the receipt to the result. Metadata CAS publishes the pointer together
  with status and lease release; an interrupted/stale attempt can leave an
  unreferenced blob, but cannot use its staging SAS to replace published output.
- Receipts contain library/photo/revision/attempt, exact blob name, content hash,
  ETag, byte count and dimensions. Qualification requires the current revision;
  serving can retain the previous published derivative for the same photo while
  reprocessing. Cross-photo pointers fail validation. Invalid/missing receipts
  cannot commit successful thumbnail/preview statuses in the opt-in path.
- Private media access, proxy serving, public media serving, gallery summaries,
  listing fields, sort indexes and compact access indexes preserve the pointers.
  An original's blob identity remains separate. Token-mode thumbnails load
  directly from storage. A versioned thumbnail name cannot be reused to derive
  an original or preview URL; those tiers use the existing access resolver.
  Legacy physical-name mappings, MIME types and ordinary unfenced writes remain
  compatible.
- Artifact capture verifies the derivative receipts and copies complete face
  inputs, including separate embeddings, into immutable snapshot rows. A valid,
  source-bound complete lookup is mandatory even for zero faces. Face/embedding
  attempt mismatches, invalid vectors, wrong photo/revision, incomplete lookups
  and count mismatches fail capture. A manifest is created only after every
  snapshot is durable. Capture is bounded to 128 faces, with a 60,000-byte UTF-16
  property budget per snapshot; larger inputs need the future paged protocol.
- The source metadata CAS records the manifest reference. Completion-intent
  schema 3 captures that reference and exact derivative receipts, so later
  source generations cannot erase the proof attached to an older pending
  intent. The backlog is bounded by both 16 entries and 60,000 UTF-16 bytes;
  reaching a bound blocks the commit instead of evicting recovery evidence.
- The inbox verifies referenced manifests and snapshots before setting
  `artifactReceiptQualified=true`. `completionQualified` remains false:
  artifact qualification alone cannot release a wave or authorize clustering.
- If receipt storage fails after model/result application finishes, the fenced
  partial commit preserves completed aggregate fields and immutable derivative
  pointers. A queue retry can capture/commit the receipt without rerunning
  models. The worker's no-model recovery path retains the legacy face-clustering
  trigger while live cutover is off. If aggregate application itself fails,
  opt-in processing retries incomplete claimed steps rather than persisting a
  done status without its output fields.

## Remaining release gates

1. Allocate/freeze the required-step and model contract in a real processing
   generation, including forced reprocessing and supersession. An allocated
   revision string or an artifact-complete manifest is insufficient by itself.
2. Qualify/migrate legacy duplicates and retained curated faces explicitly;
   missing provenance or inconsistent counts cannot be inferred as no work.
3. Connect actual stage claims/releases, immutable manifest consumption,
   per-face assignment decisions, real projection publication and independent
   recovery, as listed in checklist items 3–6.
4. Integrate generation-aware permanent deletion, library cleanup and orphan
   retention for staging/published blobs and immutable receipt partitions.
   Namespace paths carry a library hash to permit scoped cleanup. Referenced
   snapshots/output must survive wave retries; unreferenced output must not
   become permanent storage. This lifecycle integration is required before
   enabling the new path outside controlled validation.
5. Measure extra bounded JPEG verification reads and snapshot I/O in rollout,
   alongside the activity/sleep behavior from checklist item 7.

The feature flag remains off. This work does not claim completion of item 2 or
of the seven-item pipeline migration.

## Validation

The final commands and results are recorded below. Counts for overlapping
selections must not be added together.

312 focused backend tests passed:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_upload_waves.py \
  backend/tests/test_processing_receipts.py \
  backend/tests/test_processing_artifacts.py \
  backend/tests/test_processing_completion.py \
  backend/tests/test_processing_fence.py \
  backend/tests/test_metadata_write_concurrency.py \
  backend/tests/test_upload_wave_shadow.py \
  backend/tests/test_ipwork_dispatch.py \
  backend/tests/test_ipwork_race_guard.py \
  backend/tests/test_sort_index.py \
  backend/tests/test_photo_access_batch_deleted_fallback.py \
  backend/tests/test_proxy_thumbnail_preview_fallback.py \
  backend/tests/test_raw_native_preview.py
```

82 frontend tests and TypeScript checking passed:

```sh
npm --prefix frontend run typecheck
npm --prefix frontend test -- --run \
  src/services/mediaToken.test.ts \
  src/services/uploadWaveOperations.test.ts \
  src/services/uploadWaveSync.test.ts \
  src/services/uploadWaveClient.test.ts \
  src/components/AppServicesProvider.upload.test.tsx \
  src/services/indexReadyPush.test.ts
```

The media-token test fixture now supplies isolated in-memory localStorage,
matching the other browser tests, so it works when Node exposes no default
localStorage. The new test verifies that versioned thumbnail paths cannot be
mistaken for preview/original paths.


The full regression run also exposed starvation in immediate retries on the
wave control row: one concurrent admission exhausted all eight attempts. The
retry loop now yields with bounded random backoff (1–50ms), preserving the
existing conditional writes and retry limit. After this fix, 100 rounds of four
writers admitting 12 sessions each completed with one wave and exact counts:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python - <<'PY'
from concurrent.futures import ThreadPoolExecutor
from test_upload_waves import AtomicTable
from upload_waves import ShadowWaves
for run in range(100):
    store = ShadowWaves(AtomicTable(), 'library')
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda i: store.open_session('session-' + str(i), 0), range(12)))
    assert len({row['waveId'] for row in rows}) == 1
    assert store.status(rows[0]['waveId'])['openSessions'] == 12
print('100 rounds / 1,200 concurrent admissions passed')
PY
```

Final full backend run: **3,572 passed, 24 failed, 14 skipped and 28 setup
errors**. The failed/error identities exactly match the earlier baseline, with
no additions or removals. The additional concurrent-admission failure from the
intermediate run is resolved. The full suite remains red for the baseline
issues documented in the integration review.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

`git diff --check` passes. No deployment or live cutover was attempted.
