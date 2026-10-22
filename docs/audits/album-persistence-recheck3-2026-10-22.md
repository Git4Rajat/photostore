# Album persistence re-audit, pass 3 — 2026-10-22

All 15 previously preserved adversarial cases pass. Two additional integration cases fail: one P1 and one P2.

Scope: all current uncommitted application changes in backend/app.py, AlbumsPage.tsx, store.tsx, new albumOperations.ts, and the recovery runbook. Historical audit and incident artifacts were treated as supporting evidence. This was a local code and test audit; no live browser/Azure E2E run, deployment, queue operation, or data repair was performed. No application or existing test files were changed.

## 1. [P1] A temporary source read failure now persistently hides an existing album

New code: frontend/src/mockups/prototype/store.tsx:1789–1803, especially beginAlbumDelete(id) at line 1802.

openAlbum now interprets every detail GET 404 as a confirmed deletion, stores a durable tombstone, and removes the album from the list. However, backend/app.py:8659–8665 catches every table read exception and returns None; backend/routes/albums.py:209–211 translates that into 404. A transient storage/network failure therefore produces the same response as an actual deleted album.

Reproduced using the actual get_album route with an existing FakeTable row and a ServiceResponseError from get_entity. The row remains in storage, but the route returns 404 (the new test expects a retryable 503 and fails). The previously preserved StoreProvider 404 case now passes by verifying that a 404 removes the row and keeps it hidden through subsequent list reads. Together these exercise the backend response and the new destructive frontend interpretation. The backend exception handling predates this diff; the newly persisted tombstone amplifies its impact.

The tombstone survives reloads and suppresses positive index/source list rows. Its seven-day TTL is evaluated only on scope hydration, so an open module may retain it longer. This does not delete backend data, but it makes an existing album disappear from the UI after a temporary outage.

Distinguish genuine ResourceNotFound/deleted results from transient backend failures and propagate retryable errors before trusting 404 as durable deletion evidence. Preserve real not-found handling and stale-index deletion protection.

Reproduction: album-persistence-recheck3-2026-10-22.backend.repro.py. Insert under backend/tests with the normal test environment. The frontend 404 half is retained in album-persistence-recheck2-2026-10-22.store.repro.tsx.

## 2. [P2] The creation draft overwrites newer authoritative album fields

New code: frontend/src/services/albumOperations.ts:206–210, applied to both indexed and source-list reads in StoreProvider.

Whenever a receipt exists, the overlay unconditionally overwrites a matching row's name, photoCount, and optional cover with the stored creation draft. Reconciliation remains a no-op. This protects against stale data, but also discards newer source data after another client renames the album or adds/removes photos.

Reproduced through StoreProvider: create Birthday with count 0; force an unavailable index so fetchAlbums(true) reads the source /albums endpoint; return the same album with name Remote name and count 5. The resulting UI row remains Birthday with count 0. There is no local rename pending-edit receipt in this scenario that could correct the draft. The new test fails on both name and count.

The receipt has a 24-hour hydration TTL and is not expired during ordinary reads of an already loaded scope. Thus a current source read cannot converge the list, and the stale draft can persist throughout a long open session. Opening the album directly may refresh its draft, but ordinary list refresh does not.

Use version/order-aware reconciliation or explicitly authoritative source confirmation so newer data can replace the draft while older projections cannot erase a recent save. Do not simply remove all receipt protection on one indexed sighting; the prior stale-read cases must continue passing.

Reproduction: album-persistence-recheck3-2026-10-22.store.repro.tsx, inserted inside the existing StoreProvider gallery fixture describe block.

## Validation

- Adversarial frontend run: 39 cases, 38 passed, one new source-convergence case failed (24 baseline fixture cases plus 14 earlier frontend cases plus the new case).
- Selected backend run: 98 cases, 97 passed, one new transient-read case failed (96 baseline cases plus the original legacy scheduling race plus the new case).
- All 15 earlier adversarial cases pass, including receipt cleanup enumeration, durable missing-row suppression, photo-add count retention, local rename precedence, failed-delete receipt retention, purge tombstones, explicit-ID retry behavior, and legacy rebuild follow-up scheduling.
- Temporary executable test copies were removed; focused reproductions are retained beside this report.
- Selected frontend baseline: 140/140 tests across 13 files passed. Typecheck and production build passed with dependency/runtime asset and bundle-size warnings.
- git diff --check passed.
