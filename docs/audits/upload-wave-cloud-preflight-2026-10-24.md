# Cloud qualification preflight — 2026-10-24

**Cloud qualification is not complete.** This work performs read-only Azure
inventory, adds missing deployment configuration, and prepares the controlled
run. No images were published, deployments changed, flags enabled, capability
rows written, libraries created, photos uploaded, workers interrupted or data
deleted. The environment choice is pending the user's response.

## Verified deployment

Authenticated subscription: `dxi-idp-sandbox`
(`c14d0e72-4b72-429d-bf2a-8cede087b9a8`). Resource Graph found the eight Photostore
apps in `forenklaprep1-dev-rg`, West Europe. The older `forenklaprep-rg` and
`forenkladev-rg` in historical deployment plans are not current deployment targets.

The ARM `2025-01-01` API confirms all eight apps have min replicas zero and
300-second cooldowns. Older `az containerapp list` output returned null cooldowns;
that output cannot establish configuration absence. No application HTTP request
was made and replica residency was not measured.

The currently deployed images use source tag
`55d881f1da62ceee207578f73e46ac1eda2e5066`, and are not the current uncommitted wave
implementation. The Indexer app has only its API container, with no dedicated
`index-maintenance` worker. No scheduled wave-recovery job exists. Wave execution,
source generation/completion, ownership and certificate settings are absent.
Therefore this deployment cannot qualify the new protocol as it stands.

Read-only preflight returns `configurationReady=false`, `cloudQualified=false`,
with 50 configuration blockers (individual missing settings are reported per
container). The redacted snapshot is local ignored evidence:
`.azure/wave-qualification-20261024/current-preflight-v2.json`.

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m scripts.wave_qualification \
  --subscription c14d0e72-4b72-429d-bf2a-8cede087b9a8 \
  --group forenklaprep1-dev-rg --prefix forenklaprep1-dev \
  --output .azure/wave-qualification-20261024/current-preflight-v2.json
# Exit 2 is the expected blocked configuration result.
# Use a fresh output path on every run; existing evidence is never overwritten.
```

## Deployment preparation completed

The main/resource templates now expose `enableWaveExecution=false`,
`enableNewWaveLibraries=false`, and `waveCapabilityId=''`. They supply generation
and completion gates, the certificate ID and each container's actual image as its
deployment identity. The recovery job inherits the shared execution switch rather
than hardcoding false; provisioning it still separately requires
`enableWaveRecovery=true`. The generated portal template is rebuilt.

The frontend accepts `APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED` through runtime
`env.js`; the original Vite build opt-in remains supported. This allows the same
immutable frontend image to participate in qualification without an undocumented
special build. The server's library-scoped protocol discovery and reservation
checks remain authoritative. Default runtime activation remains false.

`scripts.wave_qualification` checks all eight apps, the Indexer sidecar and queue
scaler, recovery schedule/timeout/identity, backend-only processing, exact image
pins, flags and a certificate ID derived from those exact role images. It saves
only selected nonsecret environment/configuration fields. A passing configuration
check explicitly leaves `cloudQualified=false`: data-plane IAM, durable certificate
contents, actual scheduled execution, alert ingestion, correctness, throughput
and residency remain separate measured gates.

The residency collector now requires an explicit subscription and reads cooldowns
with ARM `2025-01-01`. Missing cooldown data fails verification; it is not replaced
with an assumed 300-second value. Control-plane sampling does not wake apps.

## Next controlled run

Choose a fresh isolated group or explicitly authorize the current dev group
before deploying or performing disruptive tests. A fresh group keeps storage,
queues and capability rows separate from existing libraries. Use a new
source-bound build of all three images and pin their registry digests. Working-tree
changes are still uncommitted; the historical CI images cannot substitute for
this exact source.

Within the chosen isolated environment, create the candidate certificate from
those exact role images after local validation, and opt in only disposable new
libraries. That candidate record allows testing there; it does not assert cloud
qualification or authorize production enrollment. Production must not receive
that certificate/activation until the following evidence passes. This avoids a
circular requirement to prove execution before allowing isolated execution.

1. Run configuration preflight and verify the durable certificate, worker identity
   permissions and actual five-minute recovery job execution. Record startup and
   idle baseline before uploads; confirm portal alert ingestion.
2. Upload a representative selection larger than 900 photos, overlapping a second
   session. Check exact member identity/coverage across transport children. Exercise
   RAW, JPEG, video, no-face, many-face, duplicate, failed and cancelled selections.
   Source success must have qualified immutable receipts; failure stays explicit.
3. Interrupt at intake close, source commit, assignment decision/materialization,
   publication and acknowledgement windows. Replay queue deliveries and expired
   leases. Verify recovery, stale-owner rejection, no premature assignment and
   exact equality of assignment/publication obligations and receipts.
4. Compare assignment decisions with the reference oracle on controlled inputs.
   Exercise concurrent person/album CRUD, deletion deferral, supersession and
   shared artifact retention. Verify protected identity/curation and deletion
   boundaries before expanding the canary.
5. Compare representative latency/memory/disk/transaction measurements with the
   captured legacy baseline. Set explicit acceptable budgets before rollout;
   local unit-test counts are not throughput or cost evidence.
6. Capture inactive residency for upload, Vision, Cluster, Indexer and complete,
   using independently observed durable wave phase times. Each stage must last
   long enough for the five-minute tail plus a polling minute. Separately verify
   foreground browsing/CRUD and second-device upload-status behavior.

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m scripts.wave_residency collect \
  --subscription SUBSCRIPTION_UUID --group QUALIFICATION_RG --prefix APP_PREFIX \
  --phase vision --entered-at ACTUAL_DURABLE_PHASE_ENTRY >> /tmp/wave-residency.jsonl
# Repeat at 30-second intervals across every phase with the real entry timestamp.
PYTHONPATH=backend .venv-photostore-tools/bin/python -m scripts.wave_residency verify \
  /tmp/wave-residency.jsonl
```

## Local verification

**9 preflight/residency tests passed**, including missing sidecars/jobs, paused
execution, unknown cooldowns, wrong certificates, tagged images and redaction.
**84 affected frontend tests passed**. TypeScript, shell syntax and Bicep
compilation passed; existing Bicep warnings remain. These are local checks, not
Azure provider validation, what-if, IAM proof or deployment approval.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_qualification.py backend/tests/test_wave_residency.py
npm --prefix frontend test -- --run src/services/uploadWaveClient.test.ts \
  src/services/uploadWaveOperations.test.ts src/services/uploadWaveSync.test.ts \
  src/services/foregroundDemand.test.ts src/services/indexReadyPush.test.ts \
  src/components/AppServicesProvider.upload.test.tsx
npm --prefix frontend run typecheck
sh -n frontend/docker-entrypoint.sh
DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/photostore-bicep-extract \
  /Users/rajat.verma1/.azure/bin/bicep build deploy/main.bicep \
  --outfile /tmp/wave-cloud-arm.json
cmp /tmp/wave-cloud-arm.json deploy/azuredeploy.json
git diff --check
```

The prior full-suite baseline remains documented in the
[ownership/cleanup audit](upload-wave-live-ownership-cleanup-2026-10-24.md).

## Target selected; candidate publication awaits explicit approval

The user selected `forenklaprep1-dev-rg`. Private rollback snapshots now capture
all eight app specifications and the empty job inventory. A local isolated
candidate commit, `34e3c75db1ecaa75240dc498ac56c6c84c5057cd`, exists on
`codex/wave-qualification-20261024`; main, working files and the user's index are
unchanged. This candidate contains the implementation and preflight source.

A proposed initial rollout preserves existing settings and leaves wave
activation disabled. It adds the Indexer queue worker within the existing total
2 CPU/4Gi allocation, constrains that publisher to one replica, and prepares the
independent 0.5 CPU/1Gi bounded recovery job and storage-scoped roles. The private
review artifact is `.azure/wave-qualification-20261024/rollout-preview.json`.
These are proposed payloads, not validated or applied resources; exact secret
preservation, built image digests, provider validation, scoped what-if and canary
activation remain pending.

Automatic approval review rejected the candidate branch push because exporting
the source to the external Git repository was not explicitly authorized. No push
or alternative export was attempted after rejection. Read-only GitHub inspection
confirms `Git4Rajat/photostore` is public and the current user has ADMIN permission.
Explicit approval was requested to push only that candidate branch and dispatch
the existing three-image workflow with `publish_latest=false`. No source/image
publication or Azure mutation has occurred. Validation status remains Approved,
not Validated; cloud qualification remains incomplete.

The user subsequently explicitly authorized committing and pushing the work.
The completed working-tree source is published on a qualification branch; the
previous automatic-review rejection remains a historical event, not a denial of
this later authorization. Image publication and Azure deployment are separate
follow-on actions and have not been performed by the commit/push step.
