# Library management routing rollout

The private frontend sends library membership/invitation/rename/switch requests
to core. Archive retains library/account deletion, cleanup, downloads/exports,
public albums/media, and temporary copies of management routes for old clients.
No service resource sizes, scaling rules, or public URLs change.

## Backend first

Deploy the new backend image to every process that can create invitations or
libraries, particularly archive and core, before migrating data. Complete the
revision transition; an older live writer cannot maintain the new lookup.
Keep the existing frontend during this step: archive still supports it and
can read legacy invitations before migration.

New invitation writes atomically persist the canonical record and expiry-keyed
lookup in the same library partition. Accept/revoke update both records in a
transaction. The token pointer is written first in its separate partition; a
failed invitation transaction can leave an inert token pointer, but cannot
leave an unindexed valid invitation or reserve a membership slot.

New libraries initialize their own readiness marker. Existing libraries require
the explicit migration below. On core, a missing marker returns HTTP 503 for
pending-invitation reads instead of scanning history. Archive's compatibility
handlers can still use the historical query until the marker is present.

## Migrate existing invitations

Run from the backend runtime with its normal storage configuration and storage
identity. The script supports `STORAGE_CONNECTION_STRING`, or a table endpoint
(`TABLES_ENDPOINT`/`TABLE_ENDPOINT`), or `STORAGE_ACCOUNT_NAME` /
`AZURE_STORAGE_ACCOUNT_NAME` with DefaultAzureCredential. Table-name environment
overrides match the application.

First audit without writes:

```bash
python backend/scripts/backfill_invitation_index.py --all
```

After every writer is upgraded, populate the index:

```bash
python backend/scripts/backfill_invitation_index.py --all --apply --writers-upgraded
```

The backend image also bundles the script at `scripts/backfill_invitation_index.py`
(relative to its working directory); omit the `backend/` prefix inside that image.
Use repeated `--library LIBRARY_ID` instead of `--all` for a scoped rollout.
The migration streams historical canonical invitation rows; it writes lookup
rows only for pending, unexpired invitations and marks each library ready only
after successful enumeration/writes. It preserves invite IDs and emailed tokens.
It is safe to retry after interruption. Errors propagate and stop the run;
do not cut over the frontend until all target libraries are ready.

The lookup's RowKey encodes expiry, so core uses a PK/RK range query that skips
expired history. Closed candidates are skipped using their indexed status.
Canonical point reads validate remaining candidates, so a migration racing
with acceptance/revocation cannot resurrect a closed invitation. No migration
or fallback scan runs inside a core request.

## Frontend cutover and verification

Release the frontend after migration and verify:

- App startup `/api/library/mine`, member lists, invitations, rename, switch,
  remove-member, and leave target core and retain their authorization behavior.
- Library DELETE, `/clean/*`, `/cleanup-info` (including its trailing-slash
  alias), `/download/*`, `/export/*`, `/public/*`, and `/api/public/*` target
  archive. Public album management remains on its existing core endpoints.
- Ordinary visible Sharing visits read core metadata every 30 seconds and do
  not poll archive. During cleanup they reconcile on archive every three
  seconds. Hidden pages pause polling; focus, visibility changes and cleanup
  confirmation in another tab refresh status. Completion stops archive polling.
- Cleanup/export/deletion behavior and already-open browser sessions still work.
- Core request errors/latency and CPU/memory remain acceptable under ordinary
  Sharing activity; public-share spikes stay on archive.

## Rollback and retirement

Retain archive's management blueprint through the old-client compatibility
window. Rolling back the frontend restores its archive routing immediately.
Do not roll backend writers back to a pre-index version after migration:
those writers would create invitations without lookups and invalidate readiness.
The additive lookup rows do not require deleting or rewriting canonical records.

Remove archive's temporary `library_bp` registration in a separate release
after the compatibility window, updating isolated role tests accordingly.

## Refreshing cleanup confirmation links

The confirmation page first reads archive's
`GET /api/library/clean/confirmation-status?token=...`. This does not approve or
enqueue cleanup. The endpoint requires the authenticated recipient and matching
active library. Only an unconfirmed, unexpired request shows the confirm button.

Once approved, the token lookup is retained for status reads, while the recorded
approval prevents replay. Once cleanup starts, its request retains the original
job ID. Reloading or reopening the link resumes that job's queued/running/final
status, including after the approval deadline. Waiting approvers can also resume
the waiting screen and follow the job once the remaining approval is recorded.
An older link does not report or overwrite a subsequent cleanup's status.

Release the backend before this frontend change. Requests whose consumed token
lookups were already deleted by the old backend cannot be reconstructed from the
link; these show status unavailable rather than another confirmation prompt.

## Local verification

Run backend management/index/membership/role tests and existing library
deletion/download/export/cleanup/public-share tests. Run frontend ownership,
library-client and cleanup-polling tests, typecheck, and production build.
Shared test fixtures register all blueprints, so isolated role tests must pass
as well as endpoint tests.

Verified locally on November 2, 2026: 100 targeted backend tests (including 11
isolated role tests), 34 frontend tests, TypeScript typecheck, and production
frontend build passed. ESLint passed without errors; the shared apiClient file
retains existing warnings. Archive operation handler bodies were also compared
structurally against their originals and match. No live migration or deployment
was executed.
