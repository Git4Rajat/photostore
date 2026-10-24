# Upgrade an existing wave deployment

Use `upgrade_wave.py` for **compatible `library-wave-v1` bug-fix releases**.
This preserves the resource group, account/password, library/members, originals,
wave membership and checkpoints, source attempt counts, immutable completion,
assignment and publication receipts, and existing catalogs. No cleanup or
re-upload is needed. It does not migrate incompatible protocol/schema changes.
The release author must preserve the meaning of existing persisted records.

## Qualify and prepare

Commit the change and run **Publish public container images** on that commit,
with `publish_latest=false` for sandbox branches. Download the successful
`wave-release` artifact. The exact backend image must pass the wave, upgrade,
rollback, deployment-helper and derivative regression suites. The resulting
artifact includes `upgradeProtocol: library-wave-v1`; tags or manually assembled
certificates are not accepted. Use the helper from this qualified checkout. Run one upgrade operator at a time;
do not overlap this workflow with portal edits or another deployment.
Keep the artifact for the currently deployed release as well.

```bash
python3 deploy/upgrade_wave.py \
  --subscription c14d0e72-4b72-429d-bf2a-8cede087b9a8 \
  --resource-group forenklaprep1-dev-rg \
  --app-name forenklaprep1-dev \
  --previous-release /absolute/path/to/current/wave-release.json \
  --release /absolute/path/to/new/wave-release.json \
  --output-dir .azure/wave-upgrade
```

Default mode writes a local private plan only. Review and validate the plan,
release source/digests, target subscription, existing role identities and their
storage permissions before repeating with **`--deploy --resume`**. No owner
password, account key, resource creation, identity impersonation, or new RBAC
assignment is required. ARM snapshots can include sensitive configuration;
plans and snapshots remain mode 0600 under the ignored `.azure` directory.

## Execution and failure behavior

1. Verify the complete current release and Single revision mode; save the
   existing app/job templates. Preserve all settings, resources and secretRefs.
2. Pause recovery and all eight apps on the **old** release, using the existing
   bootstrap barrier. Public API requests temporarily return 503 except health.
   Verify every latest ready revision and that no other revision remains active;
   stop existing recovery executions. Durable wave rows survive lost queue wakes.
3. Stage every **new** image with intake, execution, enrollment and scheduled
   recovery still disabled. Verify exact images, capability, flags, latest ready
   revisions and absence of any active old revision before touching bindings.
4. Run `python -m wave_upgrade` as a one-off execution of the existing recovery
   job. It scans authoritative library policies (not merely recovery directory
   entries), verifies both immutable certificates, rejects cleanup/GC, waits for
   stage/dispatcher/source leases to drain, and changes each binding together
   with an `upgrade!<id>` receipt using an ETag-conditional transaction. It keeps
   the original activation ID. It explicitly requests fresh dispatcher revisions
   for eligible unfinished releases; completed work and source retries are kept.
5. Activate the new apps, verify all ready role revisions, then enable scheduled
   recovery last and start one ordinary recovery execution to resume the upload.

A failed phase stays recorded. Repeat the **same** command with `--deploy
--resume` after inspecting the failure. Replaying the same migration verifies its
receipts, preserves already-migrated library policies, and finishes the remaining
ones. A partial migration is never rolled back by blindly restoring app images:
keep paused and finish the saved migration. A changed upgrade ID, source commit,
source/target contract or subscription is not a valid resume.

After a completed upgrade, rollback uses the same pause/drain/audit procedure:
set `--previous-release` to the deployed new artifact, `--release` to the previous
artifact, **`--rollback`**, and **`--migrator-release`** to the qualified artifact
containing `wave_upgrade` (the newer artifact for this first upgrade). Use a new
output directory. Every library must identify the rollback target as its
immediate predecessor. Rollback never erases photos or receipts and does not
reverse already-completed processing; do not use it for incompatible schemas.
The old build may still contain the original bug.

ARM readiness plus a successful migration job proves configuration and binding
migration, not photo-processing success. Check LAW, the owner wave status, all
required publication receipts, and gallery rows after resumption. Owner login
is a normal app operation using the current password; this helper never resets
credentials to obtain access. Source decode failures remain explicit failed
members; they are not reported as successful processing.

The helper uses Azure's documented [Container App JSON Merge Patch](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/update?view=rest-resource-manager-containerapps-2025-01-01),
[job template update](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/update?view=rest-resource-manager-containerapps-2025-01-01),
and [per-execution job override](https://learn.microsoft.com/en-us/azure/container-apps/jobs).
