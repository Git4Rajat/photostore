# Album index recovery runbook

Companion to `docs/audits/red-dress-album-incident-2026-10-22.md` and
`docs/audits/birthday-album-incident-2026-10-22.md`. Use this after
confirming `albumsdirtyset` rows exist for a user with no newer
`albums`-scope rebuild (Table Storage query, Log Analytics, or the
`/api/tools/indexes/status` endpoint).

## Recovering a specific user/library

1. Prefer the self-healing path first: trigger reconciliation, which now
   also sweeps stranded album dirty rows (`reconcile_dirty_albums`, wired
   into the `reconcile` scope in `backend/app.py`):

   ```
   POST /api/tools/indexes/maintenance/reconcile
   ```

   (as that user). This is idempotent — safe to call even if nothing is
   stranded.

2. If `INDEX_MAINTENANCE_QUEUE_NAME` is configured and you suspect a lost
   queue message or worker restart rather than a never-enqueued dirty row,
   also run:

   ```
   POST /api/tools/indexes/recover-pending
   ```

   which calls `recover_pending_index_work` to re-notify any
   `index-maintenance-pending` row whose lease/wake has expired.

3. If both of the above are unavailable or you need a direct forced
   rebuild, use the manual rebuild endpoint with `index_kind=albums`:

   ```
   POST /api/tools/indexes/rebuild/albums
   ```

## Verifying recovery

Compare the live `photoalbums` table row count for the user's partition
against `get_user_albums_index(user_id)`'s row count (or the
`/api/albums/index` response `rows` count). They should match, and the
specific albums should show their correct `photoCount`/membership.

For the two incident libraries specifically: confirm "red dress" shows
**83 photos** and "dressed in yellow" shows **123 photos**, including
after a full page reload in a new browser session.

## Underlying fix

`_mark_album_dirty` (`backend/app.py`) now calls `enqueue_index_build(...,
scope='albums')` on every album mutation, so new dirty rows get a durable
rebuild intent immediately instead of relying on an incidental index read.
`reconcile_dirty_albums` catches anything left over from before this fix
(or from a queue-send failure) by checking for `albumsdirtyset` rows with
no live `index-maintenance-pending` intent and re-enqueuing. Neither
function clears dirty rows itself — only a completed rebuild
(`clear_dirty_albums`) does — so both are safe to call repeatedly without
risk of masking unindexed work.
