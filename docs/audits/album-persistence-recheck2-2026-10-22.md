# Album persistence re-audit, pass 2 — 2026-10-22

**All 11 earlier adversarial cases pass. Four additional cases fail: two P1 and two P2 findings.** The baseline tests and build checks pass. No application or existing test files were edited during this audit.

Scope: all current uncommitted backend/app.py changes, AlbumsPage.tsx, store.tsx, new albumOperations.ts, and the recovery runbook. Historical incident/audit artifacts were treated as supporting data. No deployment, live queue operation, album repair, or production data change was performed.

## Verified fixes

- Legacy rebuild completion still queues follow-up dirty album work in the original worker race.
- Failed delete now retains its pending creation receipt and restores visibility through later stale reads.
- Purge now creates a tombstone even when this browser did not perform the soft delete.
- The creation receipt survives both a legacy source-list read and one indexed sighting followed by an older index.
- A successful photo-add response updates the draft before the optional detail refresh, so the previous failed-detail reproduction passes.
- Earlier delete precedence, purge preservation, and explicit-ID retry controls continue passing.

## 1. [P1] Expiring one creation receipt can skip a fresh receipt during reload

Location: frontend/src/services/albumOperations.ts:67–69, inside the forward localStorage index loop beginning at line 59.

The loader removes expired keys while iterating localStorage by increasing numeric index. Removing the current key shifts the following key into its position; incrementing i skips that following entry. If that entry is a fresh creation receipt, it remains on disk but is absent from the cached in-memory map. A stale index cannot be supplemented with that new album for the rest of the module session.

Reproduced with persistent receipts and fresh module loading: create expired, advance time 26 hours, create fresh, reset modules while retaining localStorage, then apply an empty index overlay. Actual result is [], expected ['fresh']. The older receipt expires legitimately; the newer one does not. This is a regression introduced by the new creation TTL cleanup. The delete loader uses the same mutation-during-iteration pattern and should be checked too.

Snapshot the keys before cleanup, remove after enumeration, or iterate safely without skipping shifted keys.

## 2. [P1] Disabling reconciliation retains ghost albums even after a missing-source result

Location: frontend/src/services/albumOperations.ts:200–201; StoreProvider openAlbum 404 handling and AlbumsPage's missing-state condition.

Creation receipts now survive every indexed/source-list read and have no authoritative lifecycle cleanup wired into StoreProvider. A person on another device can delete or purge a newly created album, but this browser keeps injecting its old receipt. Even a direct album read that the frontend classifies as confirmed missing only sets isAlbumMissing; it does not remove the receipt or raw album row. AlbumsPage checks !album before showing not-found, so the injected row also defeats that explicit missing state.

Reproduced through StoreProvider: create a1 successfully, simulate a later direct source GET returning status 404, verify isAlbumMissing('a1') is true, then fetch an empty index. store.albums still contains a1, expected absent. This scenario uses no local delete tombstone because the deletion occurred elsewhere.

The 24-hour TTL is not sufficient lifecycle reconciliation; it is checked only when hydrating a new scope map. A continuously open module does not expire the receipt on subsequent reads, so the ghost can persist beyond that period as well.

Use an authoritative, token/order-fenced existence reconciliation to distinguish a genuine later deletion from stale index absence. Retire or tombstone the receipt only on that validated result and ensure the explicit missing state cannot keep displaying its old row. Preserve protection against old indexes without treating every receipt as unconditionally present forever.

## 3. [P2] A stale index row that contains the album still resets a confirmed photo count

Location: frontend/src/services/albumOperations.ts:175–178; store.tsx indexed-list overlay integration.

The updated draft now stores the successful POST's count, but applyAlbumOperationOverlay only uses it when the entire row is missing. If the index contains the album's earlier empty state, the overlay leaves that row untouched. This is precisely possible when creation and photo-addition publish separately.

Reproduced through StoreProvider: create count 0; add two photos with a successful response confirming count 2; complete a successful detail read also reporting 2; fetch an older index containing a1 with count 0. The displayed count becomes 0, expected 2. Both save and detail refresh succeeded; this is a remaining membership-protection gap rather than the old failed-detail case.

Reconcile confirmed membership/count against index payload revision or equivalent operation evidence for existing rows too. Do not blindly overwrite a genuinely newer remote membership state with an older local draft.

## 4. [P2] Injecting a creation draft bypasses the existing name-edit journal

Location: frontend/src/mockups/prototype/store.tsx:1161–1166 and the loadAlbums overlay order around line 1238.

loadAlbums applies pendingEdits to index rows, then inserts missing rows via albumFromCreateReceipt. Those injected rows never pass through the field journal. A later successful rename updates the field receipt and visible state, but not the original creation draft's name. The next stale index miss reinstalls that original name in store.albums, which AlbumsPage consumes directly. The albumById accessor still has the newer journal projection, creating inconsistent consumers.

Reproduced: create Birthday, successfully rename it to New name, then read an older index omitting it. The raw list name becomes Birthday, expected New name. Retaining creation receipts indefinitely expands the time window for this preexisting overlay-composition defect.

Apply scalar-field overlays to the final assembled collection, including injected rows, or explicitly project each reconstructed draft through pendingEdits. Preserve the current canonical name on rollback and authoritative reconciliation as well.

## Validation

- **140 selected frontend baseline tests passed across 13 files.**
- **97 backend tests passed**: 96 existing worker/index/storage/live-person tests plus the original legacy scheduling race reproduction.
- Previous adversarial coverage: **all 11 cases pass** (10 frontend, one backend).
- Added coverage: **four failures**, three actual StoreProvider cases and one persistent-storage/module-reload service case. Frontend replay executions total 38: 34 passed, four failed, including 24 baseline StoreProvider fixture tests.
- TypeScript checking, production frontend build, and git diff --check passed. Existing dependency/WASM/chunk-size build warnings remain. The page fixture emits the known JSDOM scrollTo warning independently of assertions.

No new backend defect was confirmed in the reviewed scope. These tests exercise real service, StoreProvider/page, and worker logic with controlled storage/transport/publisher fixtures. They do not establish live browser E2E, production recovery, or full-suite coverage.

## Reproduction artifacts

- docs/audits/album-persistence-recheck2-2026-10-22.operations.repro.test.ts: copy into frontend/src/services to replay the two original service cases plus the expired/fresh receipt reload case.
- docs/audits/album-persistence-recheck2-2026-10-22.store.repro.tsx: insert its three cases into a temporary copy of store.gallery.test.tsx inside the describe block. Import __resetAlbumOperationsForTests from albumOperations and reset it in beforeEach. Prior pass artifacts supply the eight earlier StoreProvider cases and backend race.

Frontend fixture command uses NODE_OPTIONS=--no-experimental-webstorage. Temporary executable copies were removed after the audit. The source changes remain uncommitted, and no fixes were applied in this pass.
