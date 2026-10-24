# Fresh wave deployment with Azure CLI

Use this path when **recreating the resource group**. It uses
`sandbox.bicep`, creates Log Analytics first, and supplies every wave flag and
image explicitly. It does not migrate an existing library or delete resources.
The portal deployment button does not perform this certificate bootstrap.

## Build the release first

1. Commit these changes and run **Publish public container images** on that
   commit. All three image builds and the `wave-release` qualification job must
   succeed. Existing images do not contain the new bootstrap module.
2. Download the **wave-release** artifact containing `wave-release.json` from
   that run. Use the matching repository checkout. Do not substitute image tags,
   reuse an older certificate, or manually invent the qualification marker.
3. Install/sign into Azure CLI with the Container Apps extension and Bicep. The
   deploying account needs subscription deployment/resource-group creation and
   the existing template's storage role-assignment permissions. No new bootstrap
   identity, role grant, or Azure resource type is introduced.

The release file records the source commit and immutable backend, frontend and
Vision image digests. Its capability ID uses the runtime's exact eight-role
contract. Publication qualification runs the wave unit suites inside the selected
backend image; this is not a substitute for Azure upload/processing qualification.

## Prepare locally

From the repository root, set the password without putting it in shell history:

```bash
read -r -s -p 'New owner password: ' PHOTOSTORE_ADMIN_PASSWORD
export PHOTOSTORE_ADMIN_PASSWORD
python3 deploy/fresh_wave.py \
  --subscription c14d0e72-4b72-429d-bf2a-8cede087b9a8 \
  --resource-group forenklaprep1-dev-rg \
  --app-name forenklaprep1-dev \
  --location westeurope \
  --admin-email 'YOUR_LOGIN_EMAIL' \
  --release /absolute/path/to/wave-release.json \
  --output-dir .azure/fresh-wave-rebuild
unset PHOTOSTORE_ADMIN_PASSWORD
```

This default mode makes **no Azure calls**. It writes two parameter files and a
resume record under the ignored `.azure` directory. Files have mode `0600`, the
plan directory `0700`; the parameter files contain the owner password. Keep them
private. Use the email that should own the new library.

## Execute after the old resource group has been removed

Repeat the same command with **`--deploy --resume`** appended. The password is
read from the prepared parameter file. The helper refuses an existing resource
group before the first deployment; removing that group is a separate operator
action. It never deletes or adopts an existing group.

| Setting | Preparation | Activation |
| --- | --- | --- |
| Images and capability ID | Exact release values | Same values |
| `waveBootstrapPending` | `true` | `false` |
| Execution and new-library enrollment | `false` | `true` |
| Processing mode | `backend` | `backend` |
| Dedicated Indexer worker and recovery job provisioned | `true` | `true` |
| Recovery work enabled | `false` | `true` |
| Browser thumbnail/preview/EXIF preparation | `true` | `true` |

The helper validates and deploys the preparation parameters, then verifies the
actual app images, flags and ready revisions. HTTP APIs return `503` except for
health; owner-library creation and Vision/Cluster/Indexer worker intake wait.
It starts an execution of the existing recovery job with its command overridden
to `python -u -m wave_bootstrap`. The job verifies the storage is fresh and
persists/read-verifies the exact certificate. The job's own command remains
configured to run `wave_recovery` for any later manual invocation, with
recovery disabled during this phase. See Azure's
[job execution template override](https://learn.microsoft.com/en-us/azure/container-apps/jobs).

Only after bootstrap succeeds does the helper deploy activation parameters.
Owner bootstrap can now create a wave-owned personal library. Subsequent new
users' empty libraries use the same certified contract. Recovery itself is
event-driven, not scheduled: it rides on real wave-lifecycle events (upload
close/resolve/repair/abandon) in the `-upload` app, not a standalone timer —
the job is `Manual`-triggered and only runs via an explicit operator
`containerapp job start` (e.g. to sweep a library that's gone fully idle with
a stuck wave and nobody visiting). No always-running coordinator is added.

Final verification checks all eight apps, the Indexer sidecar and recovery job,
logs in as the owner, requires `/upload/waves/protocol` to return `live`, and
creates/cancels a one-item session. That wave must complete with `no_work`.
It uploads no photo bytes and sends no invitations. A legacy protocol response
is a failure, even if login and ordinary uploads otherwise work.

## Interrupted runs and remaining qualification

Use the exact same command and `--deploy --resume` to retry. Resume pins the
subscription, group, app, region, owner email and complete image release. Failed
certificate seeding leaves intake blocked. Seeding may repeat over the same
certificate but refuses library data or prior wave ownership. Do not unblock
intake manually to bypass a failed bootstrap. Fix bootstrap before activation.

An activation or verification failure may leave apps active; inspect the failure
and resume. This helper does not claim transactional rollback across Azure apps.

**It does not rebind certificates for upgrades to existing libraries, and
nothing else in this deployment does either.** Each wave-owned library's
capability is permanently bound, at enrollment, to the exact image digest
running on every role (`wave_policy.require_capability` — "Library ownership
cannot be rebound"). Once this deployment is live and libraries are
wave-enrolled, **do not** redeploy `coreImage`, `webImage`, or `visionImage`
with `az deployment group create -f deploy/resources.bicep` or the portal
"Redeploy" button for any reason — not even an unrelated bug fix. Doing so
immediately and permanently fails every in-flight wave's next vision/cluster/
index attempt with a generic "This app deployment is not qualified" conflict,
since the deployed image no longer matches the sealed certificate, and there
is no automatic or manual re-certification path: `fresh_wave.py` itself
refuses to run against an existing resource group, so it cannot be reused to
fix a deployment that's already live. Until a dedicated re-certify/migrate
tool exists, the only supported way to pick up new code on a wave-active
deployment is a fresh install via this guide, not an in-place image update.

After successful deployment, qualify a real photo upload on the new owner's
library and a second user's library: overlapping sessions, Vision completion,
one clustering run per library wave, and all required publication receipts.
The cancelled-session check establishes bootstrap and admission readiness, not
real image/model execution, gallery refresh, invitations, or scale-to-zero timing.

Alerts remain opt-in as in the existing deployment template. Do not interpret
local Bicep compilation or unit tests as Azure provider validation or a deployed
success; those checks run only when `--deploy` is executed.
