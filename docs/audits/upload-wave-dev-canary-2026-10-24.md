# Dev wave activation and canary — 2026-10-24

Follow-up: the fixes were subsequently committed and deployed as `ff12cf0`.
The six-photo retry passed source, positive-face assignment, publication and
serving checks. See [the fixed canary audit](upload-wave-fixed-canary-2026-10-24.md)
for the current outcome; the failed initial run below is historical evidence.

**Result: activation worked, but the canary found two real integration defects.
Wave execution and new-library enrollment were subsequently paused. The fixes
are local and tested; they are not in the deployed images. Cloud qualification
remains incomplete.**

## Scope and provenance

The user authorized enabling and testing the dev pipeline, then offered images
from Documents. The target remained `forenklaprep1-dev-rg`, subscription
`c14d0e72-4b72-429d-bf2a-8cede087b9a8`, West Europe. Existing libraries were not
migrated. Only the disposable account/library
`uf475fb962ef5407396a218ebba3ce27b` received test data. No invitations or other
email were sent, and no existing user photos were changed or deleted.

The deployment used the exact immutable role images from source `805b50c`,
already recorded in [the initial rollout audit](upload-wave-dev-rollout-2026-10-24.md).
Activation only changed environment values on the eight apps and recovery job.
Images, secrets, identities, scale configuration, volumes, resources and the
four existing recovery storage grants were preserved. Provider validation and
what-if passed, with no resource creations or deletions.

Candidate capability
`6e0767ad613097d117f1e89affe1b845f35c37816e37dd27ef3d26093eb26d07`
was stored and read back using an existing app managed identity. It binds the
exact role images and is a dev candidate, **not** evidence of cloud qualification.
The operator OAuth identity had Table read access but lacked write permission;
account-key writes were also disabled. Neither restriction was changed. The
managed identity created the isolated test account/library using the existing
LibraryStore and enrollment code; the test then authenticated via normal login.

Activation deployment `wave-activate-805b50c` succeeded. The checked-in
configuration collector reported `configurationReady=true`, no blockers and
`cloudQualified=false`:

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m scripts.wave_qualification \
  --subscription c14d0e72-4b72-429d-bf2a-8cede087b9a8 \
  --group forenklaprep1-dev-rg --prefix forenklaprep1-dev \
  --output .azure/wave-qualification-20261024/activation-preflight.json
```

## Observed tests

| Case | Observation |
| --- | --- |
| Normal login and protocol discovery | Disposable library reported `live`. |
| Entire selection cancelled | Wave `c093c997998743b58ad2c786982c26e1` reached `complete`, `outcome=no_work`. |
| Three generated JPEGs, two overlapping sessions | Both sessions joined wave `33ec0cc4f96c4b0bb27bdde0fd8827fd`; it stayed `collecting` after uploads 1 and 2 and released Vision only after upload 3 closed intake. |
| Source processing and indexing | That wave eventually reached `complete`, `outcome=applied`, with three members, two sessions and zero failed members. All eight required publication obligations had valid exact-set receipts. Zero faces used the control-only assignment no-work proof; this did not exercise positive-face clustering. |
| Idle after completion | A control-plane-only sample observed all eight apps at zero replicas. This sample was taken long after completion; it does not establish the exact five-minute shutdown timing or residency during every stage. |
| Scheduled recovery | Recent executions with activation enabled reported `Succeeded`. This alone does not prove all repair/crash scenarios. |
| Real face inputs | Three original JPEGs from `Documents/ImageDataSet/img_celeba/000001.jpg` through `000003.jpg`, uploaded into the same disposable library across two overlapping sessions, formed wave `591e157fc2494febbd69b449d19cbd72`. A landmark-quality rejection exposed the missing attempt-bound empty lookup described below. Positive-face clustering was not qualified. |

The first applied wave took **852.445 seconds** from creation to completion.
That is an observed problematic result, not a throughput success. Diagnostics
showed missing-container errors while dirtying a legacy tag-embedding manifest,
which poisoned the Vision stage's publication guard and caused retries.

## Defects and small local fixes

1. **Terminal quality rejection lacked qualified empty-face proof.**
   `_apply_client_processing_results` could classify a landmark/quality rejection
   with no prior faces as `face_status=no_data`, but skip the face writer because
   detection was not considered a complete replacement pass. The upload-time
   empty lookup consequently retained no `sourcePhotoId` or processing revision.
   `ArtifactReceipts.capture` correctly rejected it with
   `Face lookup is not qualified for this source generation`.
   The local fix uses the existing non-destructive face writer to publish an
   attempt-bound empty lookup in this fenced terminal branch. It does not relax
   receipt checks or delete previously stored/curated faces.

2. **Wave source commits still dirtied legacy projection manifests.**
   `_commit_worker_processing_results` called `touch_user_search_indexes_state`
   even during wave-owned source processing. Its legacy tag-embedding write hit
   `ContainerNotFound`; although the legacy helper swallowed the exception, the
   enclosing publication guard remembered the error and raised
   `Index publication lease lost` at stage exit. This made already-applied source
   results retry and violated deferred publication ownership.
   The local fix applies the same source-change/durable-ownership check already
   used by metadata indexing intents, returning before legacy dirty manifests
   or dirty-file records are written. Foreground CRUD and legacy libraries
   retain their existing behavior.

Regression coverage runs the real face writer and artifact-receipt capture
against an upload-time unqualified lookup, and separately checks wave source
suppression versus foreground CRUD and legacy-library indexing.

The final focused regression run passed **163 tests**:

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_processing_receipts.py \
  backend/tests/test_processing_fence.py \
  backend/tests/test_face_reconcile_guard.py \
  backend/tests/test_face_by_filename_lookup.py \
  backend/tests/test_processing_artifacts.py \
  backend/tests/test_wave_activation.py \
  backend/tests/test_wave_runtime.py -q --tb=short
```

`git diff --check` passed. These local tests do not qualify a fixed cloud image.

## Containment and next gate

After the face failure, deployment `wave-pause-805b50c` reapplied the validated
flags-off configuration using the same pinned images. The code, worker, recovery
job, storage grants and durable library ownership remain in place. The canary's
unfinished wave is preserved; no source failure or missing artifact was converted
into synthetic success. Existing libraries retain legacy ownership.

Next: publish/build the reviewed source fixes, pin and validate the resulting
images and candidate capability, then repeat the real-face test. The old failed
terminal source needs explicit retry/reprocessing, or a fresh disposable test;
a newer binary alone must not pretend its old lookup was qualified. Resolve and
measure the initial retries before broader enrollment. Large selections above
900 files, chunk/restart assignment-oracle equality, fault injection, CRUD and
cleanup races, alert ingestion, cost/throughput budgets and per-stage five-minute
residency remain unqualified.

Private deployment parameters, test credentials, source receipts and raw logs
are retained under ignored `.azure/wave-qualification-20261024/`. They are not
part of the source audit. No new source commit/image was published in this run.
