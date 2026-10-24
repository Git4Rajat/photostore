# Approved dev rollout — 2026-10-24

The approved initial rollout to `forenklaprep1-dev-rg` completed successfully.
This supersedes the deployment-pending status in the
[cloud preflight](upload-wave-cloud-preflight-2026-10-24.md).
**Wave execution and new-library enrollment remain disabled. Cloud qualification
is not complete.** No capability certificate, canary library, test uploads or
qualification deletions were performed in this rollout.

## Source and scope

- Subscription: `dxi-idp-sandbox`, `c14d0e72-4b72-429d-bf2a-8cede087b9a8`.
- Region: West Europe; existing group `forenklaprep1-dev-rg` only.
- Source: `805b50c9f22f62efeea9a8afd743404c44cc7781` on
  `codex/upload-wave-qualification`.
- Build: [GitHub Actions 40045816182](https://github.com/Git4Rajat/photostore/actions/runs/40045816182),
  all three jobs successful. Registry content hashes, Linux/amd64 config and
  digest presence in the build log were verified. The user-started workflow also
  published `latest`; the deployment uses immutable digests.
- Deployment: `wave-qual-805b50c`, provisioning `Succeeded`, duration
  `PT58.1479695S`.

| Package | Deployed digest |
| --- | --- |
| backend | `sha256:7d6838d3717ce355a7867c3bc9421bcff01e94dd27df4c2ea5f4a29e8f11e142` |
| frontend | `sha256:bdf61bfbf3fce27bbd3efac34afc2eab4d7b1c9324ea5706ed5ef8881bd239be` |
| ipworker | `sha256:2edb5668f195a8971d422630ad3004e40867e1a415befd1193b097b3f6ef8aba` |

The scoped ARM overlay updated the eight existing apps and created one scheduled
job plus four storage-scoped role assignments. Structured what-if showed those
8 Modify / 5 Create resources, seven ignored existing resources, zero Delete,
and no changes outside the selected group. Azure validation and exact settings/
secret preservation checks passed before deployment. A fresh drift check matched
the rollback baseline immediately before apply.

## Runtime verification

All eight latest app revisions are ready and use the verified images. Existing
app principal IDs, secrets, other environment settings, ingress and volumes were
preserved. Every planned environment value, CPU/memory allocation and scale
setting matched the live ARM response.

The Indexer app now has its API container and `index-maintenance` worker. The
existing total 2 CPU/4Gi is split into API 0.5 CPU/1Gi and worker 1.5 CPU/3Gi;
maximum replicas changed from two to one. The dedicated index-maintenance queue
scaler is present. Both containers were observed Running and ready with zero
restarts in revision `forenklaprep1-dev-indexer--0000009`.

`forenklaprep1-dev-wave-recovery` has one 0.5 CPU/1Gi container, a five-minute
schedule, one replica per execution and a 240-second timeout. Its system identity
has exactly the four approved roles at the existing photo storage account:
Storage Blob Data Contributor, Storage Queue Data Contributor, Storage Table Data
Contributor and Storage Blob Delegator. The first scheduled execution,
`forenklaprep1-dev-wave-recovery-29880050`, ran from `00:50:00Z` to `00:50:34Z`
and Succeeded with execution disabled. This proves the host starts; it does not
prove enabled wave recovery, failure injection or artifact processing.

HTTP checks passed:

- Web `/` and cache-bypassed `/env.js`: 200.
- Runtime config: backend processing and `uploadWaveExecutionEnabled: false`.
- Unauthenticated core `/api/photos` and Upload `/upload/waves/protocol`: 401.

[Dev web](https://forenklaprep1-dev-web.yellowbush-74372508.westeurope.azurecontainerapps.io).
These verification requests intentionally wake serving apps; their replicas are
not evidence of inactive residency.

## Approval and private evidence

The initial create command was rejected before execution by automatic approval
review. The user then explicitly approved the exact eight app updates, Indexer
worker, scheduled recovery job and four storage-scoped role grants. The same
validated rollout was applied after a fresh no-drift check; no alternate action
bypassed the rejection.

Private, ignored evidence lives under `.azure/wave-qualification-20261024/`:

- `verified-images.json`, `build.log`, `pinned-validation.log`, `what-if.json`,
  `scope-proof.json`.
- `before-containerApps.json`, `before-jobs.json`, `rollback-apps.json`.
- `deployment-result.json`, `after-containerApps.json`, `after-job.json`,
  `deployment-verification.json`, `http-smoke.json`.

Secret-preserving deployment parameters and snapshots stay local and uncommitted.
No additional source commit was built or deployed during verification.

The next gate is the separately controlled disposable-library canary: certificate
setup and activation, upload/overlap/900-file coverage, crash/retry and stale-owner
rejection, assignment/publication correctness, cleanup/CRUD races, representative
resource budgets and measured inactive residency. The approved initial rollout
leaves all of those activation gates off.

## Subsequent activation and canary

The user later authorized dev activation and testing. Configuration activation
and the cancelled/no-face tests passed, but real-image testing found receipt
and legacy-dirty-manifest defects. Activation/enrollment were paused again.
See [the canary audit](upload-wave-dev-canary-2026-10-24.md) for observed results,
local fixes and the remaining release gate; the original flags-off deployment
report above is historical evidence, not full cloud qualification.
