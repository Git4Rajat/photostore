# Fixed dev canary — 2026-10-24

This follows [the first canary and its two defects](upload-wave-dev-canary-2026-10-24.md).
Source fixes were committed and pushed as
`ff12cf0690e371b0a31f1ff0da77a5a438dc3a71` on
`codex/upload-wave-qualification`. The documented 163 focused tests passed for
this exact source. GitHub Actions
[run 40070787621](https://github.com/Git4Rajat/photostore/actions/runs/40070787621)
completed all three image jobs successfully with `publish_latest=false`.
Registry manifest bytes were independently hashed, linux/amd64 configurations
checked, and each digest matched the build logs. No shared `latest` build tag
was used.

| Package | Immutable image digest |
| --- | --- |
| backend | `sha256:90a18eb4bb52fa6b139f0e7d3904775c0b8b08fdec8ab335f90a57778a45859a` |
| frontend | `sha256:19706a74a66cb1ff8d97e7e071cb69614e71e7439ecb1d4857c0233266c69983` |
| ipworker | `sha256:1f966d91c229b6cabceabc27f655deae7bf2dad7553fe74324074dfa8ede4243` |

The user authorized the next commit/build/deploy/test step. The target is the
existing `forenklaprep1-dev-rg`, subscription
`c14d0e72-4b72-429d-bf2a-8cede087b9a8`, West Europe. The eight existing apps and
recovery job use the new images; general new-library enrollment stays disabled.
Only fresh empty disposable library `u6b32972c0af8404682f984a4aaab7bd9` was
explicitly enrolled by the existing managed identity, using the normal enrollment
checks and a candidate capability bound to the exact new role images:
`49cc07dae8347bc64bb3b29191d8746a6e2ac99c8dae28dbf52488223bc38764`.

The original isolated canary's recovery-directory entry was backed up and
retired with a genuine ETag. Its original ownership, photos, receipts and
incomplete wave remain unchanged; none were declared completed. This prevents
the new candidate scanner from trying an intentionally retired test whose
certificate binds older images. This is a test retirement action, **not** a
supported general upgrade procedure for existing wave-owned libraries. Safe
image/certificate upgrades for real enrolled libraries need qualification before
general enrollment.

Provider validation/what-if passed with no resource creations or deletions.
Images and wave environment values are the only intended app changes. Existing
credentials, principals, storage grants, ingress, volumes, scaling and resource
allocations are preserved. The candidate record was written/read back before
deployment. No email was sent and no existing user photos were changed.

Private evidence and credentials are under ignored
`.azure/wave-fix-20261024/`. Deployment `wave-fix-ff12cf0` succeeded. Replacement
revision verification and six-photo canary results are recorded below when
observed; this build/deployment alone is not full cloud qualification.

## Observed fixed canary result

All eight replacement revisions became ready. Exact deployed images/env, job
settings, existing secrets/principals, storage grants, volumes, ingress and scale
settings passed live verification. Web/runtime configuration checks passed and
unauthenticated core/upload requests still returned 401. General new-library
enrollment remains false; the configuration collector's only general-rollout
blockers were the deliberately disabled core/archive enrollment switches.

Normal test-account login and live protocol discovery passed. Cancelled wave
`8af870da497d4080b71e4d18fe0b256e` completed with `outcome=no_work`.

Six original JPEGs `img_celeba/000001.jpg` through `000006.jpg` were uploaded
directly to Blob Storage through the returned upload SAS URLs and finalized
through authenticated Upload API routes. The two sessions shared wave
`83adf229bc164715b7ab38e9c1528e59`. It remained `collecting` after uploads 1–5
and released Vision after upload 6 sealed intake.

The wave completed with `outcome=applied`, **six members, two sessions, zero
failed members**, in **46.355 seconds** from wave creation to durable completion.
This is a small functional smoke result, not a controlled throughput comparison
with the earlier 852.445-second failing/retrying run.

Independent checks verified:

- All six members settled with qualified immutable completion receipts.
- Each source's face lookup is complete and bound to its photo/source revision.
- The previously failing input 000003 produced `no_data` after
  `landmark_detection_failed`, with an explicitly qualified empty lookup and
  zero immutable face snapshots. Receipt checks were not weakened.
- Five positive faces entered the frozen assignment manifest; all five immutable
  decisions have matching applied markers. The manifest binds
  `canonical-faiss-v1` and the fixed wave revision.
- The full eight-obligation publication set has valid exact-ID receipts; its
  coverage proof matches the wave's required publication ID and assignment ID.
- Authenticated serving returned all six gallery photos with no partial-index
  flag and five People entries, matching the assignment person IDs and face
  counts.
- The captured 202-message Vision log sample contains no errors or either of the
  two earlier failure signatures. This bounded sample is not a proof about every
  log or every possible failure.

Durable successful stage receipts are ordered Vision → Cluster → Indexer. The
complete state was independently read from Table Storage. Foreground requests
were then stopped before the settled idle observation.

All six source-attempt counters are exactly 1. Vision, Cluster and Indexer leases
all succeeded on epoch 1. The natural scheduled recovery execution
`forenklaprep1-dev-wave-recovery-29880405` succeeded and its telemetry recorded
`disabled=false`, one library, two waves and **zero errors**. Thus the source
path in this smoke did not rely on the previous retry delays, and the independent
scanner ran against the current candidate rather than staying disabled.

The remaining gates are broader than this smoke: large selections above 900 files,
chunk/restart oracle comparison in cloud, crash/duplicate/stale-owner injection,
foreground CRUD and cleanup/reprocessing races, alert ingestion, representative
cost/throughput budgets, safe upgrades of already-enrolled libraries, and settled
residency observations during each long-running stage. New-library enrollment
remains off while those general-rollout requirements are incomplete.

## Residency measurement correction

The first settled sample counted one core and one cluster replica. This was not
accepted as a residency pass. Investigation found that the replica-list endpoint
still returned the old cluster container as `Running`, even though worker logs
showed its clean stop and raw ARM revision state reported `ScaledToZero` with
`replicas=0`. Thus counting replica-list rows is not a reliable live residency
measurement.

During diagnosis, the installed Azure CLI 2.68 console-log validator also pinged
the app's public root URL to activate a replica. Core access logs recorded that
HTTP ping at 06:54:49 UTC; this invalidated the original foreground-idle clock.
No application change was made to hide the observation or force min replicas.

The operator collector now reads each active revision's live `properties.replicas`
from ARM 2025-01-01, handles scoped pagination, and rejects missing/invalid counts.
It issues no replica-list, log-stream, SSH or application HTTP commands. The
collector-only change does not alter the deployed runtime/source fixes in ff12cf0.
Its focused tests passed **9 tests**:

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave_residency.py -q --tb=short
```

The final idle measurement must allow a fresh 360 seconds after the diagnostic
HTTP wake. Earlier stale-inventory samples are retained as diagnostic evidence,
not as proof of either successful or failed physical residency.

## Final observed state

The corrected non-waking sample passed after **385.961 seconds** without
foreground activity following the diagnostic ping. All eight apps had
`replicas=0`, `minReplicas=0` and `cooldownPeriod=300`. The wave was independently
complete. This qualifies the observed post-completion idle phase only; the short
six-photo run cannot establish settled residency during each processing stage.

The fixed source `ff12cf0` remains deployed. Wave execution is enabled for the
explicitly enrolled canary; general new-library enrollment is **disabled** on all
apps and the recovery job. Existing non-wave libraries retain their original
path. Cloud qualification for broad enrollment remains incomplete.

The operator-only collector correction and this audit are committed separately
from the deployed runtime fixes. No additional image build is needed to run the
local qualification collector; no further live resource changes were made after
the successful fixed runtime deployment.
