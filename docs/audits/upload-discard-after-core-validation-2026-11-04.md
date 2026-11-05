# Validation: user-return recovery followed by Retry or Discard

Validated against the current uncommitted source using mocked storage/transport.
No running Azure resources or production application code were changed.

## Directory deletion, tested separately

Core's `Sessions.settle` atomically removes only the entry in
`photouploadsessions` and creates the notification. The authoritative session,
wave and registered item rows remain in the existing jobs table.

Three targeted integration tests confirmed:

1. After Core removes the directory entry, the original session and canceled
   manifest items remain, and POST `/upload/waves/live/sessions/<id>/discard`
   still returns the abandoned session successfully.
2. Retry can open a linked successor, get a distinct wave with the old wave as
   predecessor, register unfinished work and pass source admission.
3. POST `/upload/cancel` can delete tracked pending metadata and request staged
   blob deletion without the directory entry, using filename/upload identity.

Directory deletion therefore is not the cause of the current cleanup gap.

## Full user-return flow

- Core reconciliation runs on authenticated user traffic. Once stale closure
  and handoff finish, it durably publishes the issue notice.
- The notice offers Retry. Actual retry requires a matching local checkpoint
  plus cached bytes/file handles; otherwise it asks the user to reselect.
- The global paused-upload bar offers both Retry and Discard only when the
  browser still has a pending local checkpoint. The durable notice itself has
  no Discard action. Returning on another device does not recreate that local
  checkpoint. The existing mount path also automatically cleans/expires local
  checkpoints older than 24 hours, removing the paused-upload actions.
- A successful Discard preserves finalized photos and clears unfinished local
  records. Existing completed-metadata cleanup tests passed.

## Reproduced gaps

1. **Server Discard after Core abandonment skips physical cleanup.** Core calls
   `abandon` without an artifact-cleanup callback. Later server Discard reaches
   `abandon`, which immediately returns for an already-abandoned session. A
   staged pending metadata/blob reservation remained after the successful
   Discard response in the reproduction. This also occurs while retaining the
   authoritative session/manifest; restoring the directory row would not fix it.
2. **Browser cleanup is best effort.** It skips files without `uploadId` (for
   example, an init response/checkpoint was lost), swallows cleanup transport
   errors, and does not inspect the HTTP 207 / `success:false` response. If wave
   abandonment succeeds, Discard then clears the retry/cleanup checkpoint and
   displays success even when artifact cleanup failed. Both network failure and
   partial cleanup response were reproduced.
3. **Preview artifacts are omitted.** The current delete helper requests original,
   thumbnail and metadata-packet deletion, but not `preview/<blob>.jpg`, despite
   upload initialization issuing a target for that object.

## Validation results

- Backend: 7 passing checks (including existing finalized-photo preservation
  coverage), 2 expected failures capturing missing post-abandonment cleanup and
  omitted preview deletion.
- Browser: 3 passing focused checks, 2 expected failures capturing checkpoint
  removal after cleanup transport failure or partial-success response.
- Temporary probes: `/tmp/photostore-upload-discard-validation/`. The temporary
  browser test copy was removed after running; production code was left intact.

## Required contract to complete the proposed design

Keep session/manifest and exact cleanup identities usable after wave closure.
Treat explicit Discard as a separate, replayable cleanup operation, including
for an already-abandoned session. Confirm all artifact deletions before clearing
the browser checkpoint or durable cleanup-pending state. Offer server-backed
Discard alongside Retry in the notice so the action does not depend on a local
checkpoint surviving, and reconcile the existing 24-hour local expiration with
the intended user-choice flow. This fits browser + Upload + Core + Table Storage;
it requires no expiry queue or sidecar.
