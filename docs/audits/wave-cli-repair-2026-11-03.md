# Existing-wave CLI repair — November 3, 2026

Applied to storage account `forenklaprep1devmqwhngus` in subscription `c14d0e72-4b72-429d-bf2a-8cede087b9a8`, resource group `forenklaprep1-dev-rg`, at approximately 08:47 UTC / 09:47 Stockholm. The user explicitly authorized repairing existing waves and assigning the connected principal account-scoped storage roles.

## Permissions

Granted principal `73312e38-b31d-4f16-bab1-a51e5a22caf6` (application `e8ce60ad-37f4-420f-8945-4406f96102e3`):

- Storage Queue Data Contributor — assignment `91712e57-5524-4bd4-ac53-32c4303a0897`.
- Storage Account Contributor — assignment `a0c21bef-e2ba-4a90-aa04-98e227e87564`.

Both are scoped to this storage account only. Storage Table Data Contributor already existed. Access used Azure AD credentials; no storage keys were retrieved.

## Reviewed plan and executed repair

The dry-run CLI plan confirmed vision was stopped, the library had no cleanup/GC fence, the wave's vision outbox matched its current release, and both stage and dispatcher ownership had expired. Backups were saved before mutations.

For blocking wave `6d61cb0ac5214365995d4288b519ea16` (10,207 members):

- Previous dispatcher: `failed=true`, six charged attempts, revision `65466033-6091-4ad6-b7f8-867d1ea3e804`.
- Reissued the dispatcher through the same `WorkStore.request(advance=True)` primitive used by the owner's retry-stage endpoint.
- New dispatcher: `failed=false`, revision `728026fd-7f06-402a-bc1d-8483b23de859`. The next claim is attempt **1**, because failure accounting is revision-scoped. The historical raw attempt counter was preserved.
- Published one matching `upload_wave_stage` resume message: `e8e3d2e8-9e31-4363-9bd6-ef9e1fd30196`.

For tag maintenance:

- The visible queue contained ten `tag_index_maintenance` messages for the same durable library/key. Some had already been delivered hundreds of times.
- Preserved the tag intent and its revision; changed its retry to `wave_wait` with a one-hour delay.
- Published one replacement delayed receipt, `2e6e496b-9eb7-4449-88e3-0c2236a7f672`, and durably recorded its wake reservation.
- Only after that replacement existed, received a bounded queue batch and deleted the ten exact duplicate tag receipts. The received wave-stage message was made visible immediately and retained. Unknown, photo and other stage messages would likewise have been retained. No queue-wide clear was used.

## Verification and remaining state

Compared all wave rows before/after by SHA-256 and compared the full vision execution row. Both were unchanged, including the source-scan checkpoint. No source retry counters, completion/artifact/assignment receipts, library contents or published indexes were changed.

The four successor waves remain correctly gated:

| Wave prefix | Members | State |
|---|---:|---|
| 88397a60 | 9,980 | waiting_predecessor |
| 5c061254 | 10,035 | waiting_predecessor |
| 01937bc4 | 8,661 | waiting_predecessor |
| 04b165a3 | 2,506 | waiting_predecessor |

This repairs dispatch and duplicate queue state; it does not complete the remaining inference. Vision was left **Stopped**. The tested code fix remains local, and the latest published image checked was the old `d756032` image. Deploying the qualified fix and resuming vision are still required to process the backlog. Successors must be released through normal qualified predecessor completion, not forced complete.

Evidence and the exact reviewed/applied task script are in ignored directory `.azure/upload-wave-review-20261103/`: `repair_existing_waves.py`, `repair-plan.json`, `repair-before.json`, `repair-published.json`, `repair-received.json`, `repair-deleted-duplicates.json`, `repair-result.json`, `repair-roles-verified.json`, `repair-app-verified.json`, and `repair-queue-verified.json`.

The script defaults to a read-only plan and requires `--apply` for changes. Saved queue payloads and row backups are private (mode 0600). Temporary CLI credential copies were removed after verification.

## User rollout verified — 09:59 Stockholm

The user subsequently rolled out and started vision. Read-only checks confirmed:

- Running, healthy revision `forenklaprep1-dev-vision--0000063`, with one replica and no other active revision.
- Image `ghcr.io/git4rajat/photostore-ipworker:589569472dfc61af1527d143918ca85c9e99d98c`, matching the committed vision contention/maintenance fix.
- Repaired wave `6d61cb0` started at 09:57:22 Stockholm on dispatch attempt 1.
- Its first completed pass succeeded in 101.565 seconds, recording 118 source attempts, zero source errors and 126 qualification completions. Qualification includes existing receipt replay; these counts are not a whole-wave completion percentage.
- A second pass started. The durable source checkpoint advanced beyond the saved repair checkpoint, and durable wave progress reached 09:59:03 Stockholm.
- No mutation-contention/coordination-retry event appeared in the inspected post-start telemetry. The observation covers only the initial passes, not the whole backlog.
- The 10,207-member wave is actively processing (its durable lifecycle label remains `processing_pending` during bounded passes); the four successors remain `waiting_predecessor`.

Exact post-rollout query results are saved in the same ignored evidence directory under `post-rollout-*`. Temporary CLI credential copies were removed after these checks.
