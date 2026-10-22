# Uncommitted album persistence and scheduling audit — 2026-10-22

**Six actionable findings, confirmed by seven failing adversarial cases.** The selected existing suites pass. The changes improve the ordinary creation and direct-link paths but do not yet satisfy the complete lifecycle requirements.

Scope: current uncommitted backend/app.py, frontend AlbumsPage.tsx and store.tsx, new albumOperations.ts, and recovery runbook. Earlier incident reports are supporting evidence. This pass used code review and controlled service/StoreProvider/page/worker tests; no live environment changes or recovery were executed. No implementation or existing test files were edited.

## 1. [P1] Mutations during an active legacy album rebuild still have no follow-up job

Location: backend/app.py:8690; interacting legacy active-job guard in enqueue_index_build and dirty acknowledgement in _run_index_build_job.

The new mutation hook calls enqueue_index_build, but the legacy path returns already_active while the album job is running, without recording a newer work revision or scheduling another wakeup. If the active job already read its inputs, its publication misses the mutation. Conditional dirty acknowledgement correctly preserves the newer dirty row, but the job then becomes done and no worker message remains to consume that row. The new reconciler runs only when reconciliation is explicitly invoked; it does not itself establish an automatic follow-up here.

Reproduced with the actual legacy worker and enqueue logic: seed a dirty album, start an albums job, save a newer album version from inside the simulated publication step after the dirty snapshot was captured, and finish the job. The dirty row remains, the job is done, and the captured queue sends are empty. This is an incomplete fix of the missed-rebuild incident, not a claim the mutation hook introduced the existing active-job guard. The earlier live investigation found this deployment using the legacy queue path.

Record a follow-up intent/revision even when coalescing into active work, or recheck remaining dirty rows after acknowledgement and durably schedule another pass. Recover queue-send failures automatically rather than depending solely on a manual maintenance call.

## 2. [P1] A pending creation overrides a deletion tombstone

Location: frontend/src/services/albumOperations.ts:154–155.

applyAlbumOperationOverlay first filters deleted rows, then appends missing creation receipts without excluding tombstoned IDs. beginAlbumDelete also leaves the create receipt in place. Creating an album and deleting it before the index sees it therefore causes the supposedly deleted album to be injected again on the next stale/empty read or reload.

Reproduced directly: beginAlbumCreate(a1), beginAlbumDelete(a1), then overlay an empty snapshot. Actual result contains a1, expected empty. Deletion must dominate creation, while retaining enough operation state for a genuine failed-delete rollback.

## 3. [P1] Purge clears the protection needed to keep a permanently deleted album hidden

Location: frontend/src/mockups/prototype/store.tsx:1666.

purgeAlbum removes the deletion tombstone immediately, before the request succeeds, and never reinstates it after a successful purge. Permanent backend deletion does not remove that ID from an already downloaded stale index. The next album read can therefore display the purged album again, recreating the incident's ghost entries.

Reproduced through StoreProvider: a tombstoned album is absent while an old index still contains it; complete purge successfully; force the same old index read. The album reappears. Retain a deletion/purge tombstone until the indexed projection is safely reconciled; successful purge is not a reason to expose stale rows.

## 4. [P1] A legacy source read retires creation protection before the index confirms it

Location: frontend/src/mockups/prototype/store.tsx:1176; receipt reconciliation at frontend/src/services/albumOperations.ts:161–164.

The legacy /albums fallback reads current source rows, but calls the same reconcileAlbumCreateReceipts method as an indexed read. Mere presence removes the receipt permanently. If that fallback sees the new album while the index is unavailable, then a later available-but-old index lacks the album, there is no receipt left to preserve it. Presence alone also has no projection version floor against later older snapshots.

Reproduced through StoreProvider: create a1; make the index fail so /albums returns a1; fetch again from an old empty index. The album disappears. A separate service reproduction confirms that after any one presence confirmation, an older absence drops the created row.

Keep authoritative source confirmation separate from indexed convergence. Do not clear index protection on a source-list fallback, and retain sufficient version/order evidence to reject later older projections.

## 5. [P2] Saved membership count reverts to the pre-add creation count

Location: frontend/src/mockups/prototype/store.tsx:1787 and albumFromCreateReceipt/applyAlbumOperationOverlay; addPhotosToAlbum does not update the receipt.

The creation receipt captures photoCount from the empty album response, normally zero. Subsequent confirmed photo additions update React state but never update that receipt. When a stale index omits the new album, the overlay reconstructs it from the original empty draft. The album can remain visible but show zero photos after navigation/reload despite a successful save.

Reproduced through StoreProvider: create with photoCount=0, successfully add two photos and resolve its detail with photoCount=2, then fetch an old index lacking the album. Actual count becomes 0, expected 2. Persist confirmed membership/count progress and apply it while indexes lag; retirement must establish the relevant membership state, not just ID presence.

## 6. [P2] A transient explicit-album fetch failure leaves an endless loading screen

Location: frontend/src/mockups/prototype/pages/AlbumsPage.tsx:96–102 and 216–220; openAlbum error handling in store.tsx.

A 503/network failure leaves albumNotFoundIds false and inserts no album. albumResolving therefore remains true. The effect depends only on that unchanged boolean and the unchanged explicit ID, so it does not retry when loading finishes. The resolving branch has only a spinner and no failure/Retry or navigation control. The user cannot recover from this view without leaving/reloading, despite the comment promising a resolvable retry state.

Reproduced by rendering the actual AlbumsPage inside StoreProvider and rejecting the explicit ID read with status 503. The loading flag finishes, but Opening album remains rendered and no Retry button exists. Represent a completed failed attempt separately from initial loading and expose retry/back actions; keep confirmed 404 distinct.

## Validation

- Selected frontend baseline: **140 tests passed across 13 files** (journal ordering, edits/release, pending-commit regressions, reconciliation, Sort/gallery freshness and integration, StoreProvider, provider polling, push, Albums, and media-token suites).
- Selected backend baseline: **96 tests passed** across index maintenance worker, album indexes/storage, and live person albums.
- Adversarial frontend replay: **six failures**, plus 24 baseline fixture cases passed. One issue (premature receipt retirement) has both service and StoreProvider reproductions.
- Adversarial backend race: **one failure**. Thus seven failing cases confirm six findings.
- TypeScript checking, production frontend build, and git diff --check passed. Existing dependency/WASM/chunk-size build warnings remain.

No browser E2E or live queue/publication test was repeated. Controlled fixtures exercised actual services, StoreProvider, AlbumsPage, and worker/queue logic. The worker publisher was stubbed to deterministically insert a newer mutation after the job captured its inputs. The page test emits the existing JSDOM scrollTo-not-implemented message; its failure is the missing retry UI after a completed failed read.

## Reproduction artifacts

- album-persistence-2026-10-22.operations.repro.test.ts: copy into frontend/src/services to run its two service cases.
- album-persistence-2026-10-22.store.repro.tsx: insert the four cases into a temporary copy of store.gallery.test.tsx inside its describe block. Import AlbumsPage, screen from Testing Library, and __resetAlbumOperationsForTests/beginAlbumDelete from albumOperations. Reset album operations in beforeEach alongside the existing pending-edit reset.
- album-persistence-2026-10-22.backend.repro.py: copy into backend/tests and run pytest for its one worker race.

Use NODE_OPTIONS=--no-experimental-webstorage for frontend fixtures. All temporary executable copies were removed after testing; the artifacts above remain outside normal test discovery.

## Scope remaining from the work-item list

This diff does not yet implement persistent operation progress/failure notifications, safe step-specific retry/resume, complete membership receipts, People mutation overlays for Albums, stalled-operation status, or the album-detail performance work. Those are outstanding work items, distinct from the confirmed implementation defects above. The runbook describes manual recovery; no live recovery was executed in this audit. Its verification text also references a rows count in /api/albums/index, which actually returns manifest/URL metadata rather than the full rows; verify the downloaded index or a corresponding diagnostic instead.
