# Album persistence re-audit — 2026-10-22

**All seven original executable reproductions now pass. Four additional boundary cases fail: three P1 issues and one P2 issue.** Two are remaining forms of previous findings; the optimistic-delete cleanup also introduces a failed-delete regression.

Scope: all current uncommitted application changes against HEAD: backend album scheduling/recovery and post-build dirty recheck; AlbumsPage direct-ID/error handling; StoreProvider creation/deletion/purge/detail integration; new albumOperations service; recovery runbook. Historical incident reports were treated as evidence. This audit made no implementation or existing test changes and performed no live recovery, deployment, or data mutation.

## Verified fixes

- The actual legacy worker reproduction now leaves a follow-up wakeup after a mutation arrives during the active album build. Its dirty row is retained correctly.
- Creation receipts no longer override deletion tombstones in the original overlay case.
- Purge preserves an existing local tombstone, so the original seeded-tombstone reproduction passes.
- Legacy /albums fallback reads no longer retire creation receipts.
- A successful direct detail refresh updates the pending creation draft's count.
- An explicit album read rejected with 503 now provides Retry and Back controls. The original endless-loading reproduction passes.

The old service reconciliation test calls reconcileAlbumCreateReceipts without its new authoritative argument; it now exercises the non-authoritative no-op path. The new StoreProvider test below exercises the actual indexed path, which passes true and still removes the receipt after one sighting.

## 1. [P1] A failed delete destroys the pending creation receipt

Location: frontend/src/services/albumOperations.ts:139, called optimistically from deleteAlbums in store.tsx.

beginAlbumDelete now removes and deletes the persisted create receipt immediately. StoreProvider invokes it before the delete request completes. If that request fails, the catch restores the in-memory album and clears the delete tombstone, but cannot restore the deleted create receipt. The next old index read or reload then removes the album even though its create succeeded and its delete failed.

Reproduced through StoreProvider: create Birthday, reject its delete, verify it rolls back into the list, then fetch an old empty index. The album disappears. The same receipt loss applies to a per-album failure response handled by the success callback.

Keep the prior creation receipt attached to the delete transaction and restore it on failure, or postpone its irreversible removal until confirmed deletion. Preserve deletion precedence while the request is pending, and make rollback conditional on the operation token so an older failure cannot undo a newer action.

## 2. [P1] Purge still permits resurrection when no tombstone exists in this browser

Location: frontend/src/mockups/prototype/store.tsx:1674–1687.

The fix retains a tombstone only if one already exists. Opening Recently Deleted in another browser/device (or after local storage was cleared) does not create one. Purging that server-deleted album therefore leaves no local protection against a stale active-album index, even though purge succeeded. The failure path likewise does not establish missing protection.

Reproduced through StoreProvider: load a1 in the server's trash response with no local receipt/tombstone; purge it successfully; fetch an older index containing a1. It reappears in Active. Establish deletion protection for every purge, rather than assuming this browser performed the preceding soft delete. Retain it through publication and failed purge, since the album is still soft-deleted in that failure case.

## 3. [P1] One indexed presence still removes protection against later older index reads

Location: frontend/src/services/albumOperations.ts:177–181 and store.tsx:1234.

The authoritative boolean separates source-list reads from index reads, fixing the fallback reproduction, but it supplies no payload revision or ordering evidence. Seeing an ID in one index still deletes the creation receipt completely. A subsequent older index missing that ID then removes the album. Describing the primary path as having freshness guarantees does not fence the actual StoreProvider state update or retain a version floor; the shared IndexedDB write guard also does not prevent every caller from receiving an older response.

Reproduced through StoreProvider: create a1; fetch an index containing a1 so the real true-authoritative reconciliation runs; fetch an older empty snapshot. The list becomes empty. This is a remaining form of previous finding #4, not a claim the fallback fix introduced it. Transport-controlled index fixtures establish the behavior; a live reverse-order publication was not executed here.

Retain enough receipt/projection-version evidence after convergence to reject older absence, while accepting a later authoritative deletion through explicit source reconciliation. A boolean or bare ID presence cannot establish monotonic publication order.

## 4. [P2] Confirmed photo additions still depend on a successful detail refresh to persist the count

Location: frontend/src/mockups/prototype/store.tsx:1927–1933; updateAlbumCreateReceipt currently runs in openAlbum after the detail request.

The draft is now updated only when GET /albums/:id succeeds. addPhotosToAlbum ignores the successful POST response's canonical album/count and launches a separate detail refresh. If that GET fails, stalls, or the user reloads first, the persisted draft still has the empty creation count. The confirmed membership has already saved, so a secondary read must not be required to preserve it.

Reproduced: create with count zero; POST add succeeds with two added photos and album.photoCount=2; reject the follow-up detail read with 503; fetch an index that still omits the album. Its count changes from 2 to 0. This is a remaining form of previous finding #5.

Update the receipt immediately from the successful add response (including actual added/failed membership), retaining the detail fetch for additional metadata. Track pending additions separately if they must survive interruption before confirmation.

## Validation

- Backend: **97 tests passed**: 96 selected existing tests across worker/index/storage/live-person-album behavior plus the original legacy race reproduction.
- Original frontend artifacts: **all six assertions now pass**.
- Four added StoreProvider cases: **all four fail**, as described above. The combined frontend replay ran 34 tests: 30 passed, 4 failed, including 24 existing fixture tests.
- **140 frontend tests passed across 13 selected baseline files.** TypeScript checking, production frontend build, and git diff --check passed. Existing dependency/WASM/chunk-size warnings remain.

Artifact: docs/audits/album-persistence-recheck-2026-10-22.store.repro.tsx contains the original four StoreProvider cases plus the four added cases. Insert into a temporary copy of store.gallery.test.tsx inside its describe block; import AlbumsPage, screen, and __resetAlbumOperationsForTests/beginAlbumDelete, and reset album operations in beforeEach. Existing service/backend artifacts from the prior audit replay the other three cases. Use NODE_OPTIONS=--no-experimental-webstorage for the known Node/JSDOM storage conflict. The page fixture's existing scrollTo warning is unrelated to assertions.

All temporary executable copies were removed. Browser E2E, live queues, and production resources were not touched. Passing the selected suites is not a claim that all 17 earlier work items are complete; operation notifications, safe resume/retry, full membership persistence, People-to-Album UI reconciliation, and performance work remain outside these fixes.
