# Audit of all uncommitted changes — 2026-10-22

**Status: all four findings remediated locally; validation results are appended below.**

Original audit: **four additional findings: one P1 and three P2.** These remain after the six previous findings were remediated. The previous regression cases still pass; these tests cover different ordering and pagination sequences.

Scope: current tracked diff plus untracked implementation/services/tests and related documentation against HEAD `dc96b4ba678214bdbe183de3ef66eb7218ff1709`. No changes are staged. Reviewed areas include the pending-edit journal, bounded background reconciliation, store/list/detail consumers, HTTP session isolation, index-cache version handling, People incremental coverage, index-ready publication/subscription, CSP, and optional deployment wiring. The benchmark artifacts describe local measurements and explicitly leave distributed capacity unproven.

No application implementation was changed during this audit. The only retained additions are this report and reproduction artifacts. Findings describe UI correctness; the backend source writes are not shown to lose the saved values.

## 1. [P1] Unversioned rows can permanently mask a later server change back to a previously stale value

Location: `frontend/src/services/pendingEdits.ts:129–133`, together with request selection at `:214–215`.

For unversioned rows, a new source check is demanded only when the incoming field value differs from `checkedProjection.value`. This compares data values rather than source revisions. A legitimate later write can produce the exact value previously seen in a stale projection:

1. Save rating 5.
2. The paged index still returns the old rating 2. Reconciliation reads source rating 5, retains 5 in the UI, and records checkedProjection.value as 2.
3. Another client intentionally changes the source back to 2; a subsequent rebuilt/refetched page now correctly returns 2.
4. Because 2 equals the earlier stale projection fingerprint, no new check is queued. The source-confirmed receipt continues overriding 2 with 5.

This particularly affects server-paged photo libraries, whose rows are passed to the journal without a publication/entity version. Ready notifications and repeated page fetches do not change that fingerprint. The persisted checkedProjection also carries the problem across reload.

**Reproduced:** after a source confirmation at 5, advance time ten minutes, reset/reload the modules from browser storage, and consume the later page at 2 with source configured to return 2. The UI overlay still returns **5** instead of **2**. This is not the one-second check-coalescing window.

Suggested correction: provide authoritative entity/publication revisions on paged responses, or use a bounded revalidation epoch for visible unversioned receipts on publication/freshness events. Equal field values cannot distinguish an old projection from a later write with the same value. Preserve the bounded network budget while allowing this state to progress.

## 2. [P2] An older thumbnail generation erases a saved edit before version protection runs

Location: `frontend/src/services/pendingEdits.ts:187–199`; caller `frontend/src/services/localSortIndex.ts:158–160` (and its decompression branch).

`observePhotoGeneration` interprets any differing physical thumbnail as a replacement upload and deletes the receipt. The loader invokes it before applying the payload's publication version, and does not pass that version into the generation observer. Consequently an older snapshot containing the previous upload's thumbnail is treated as a newer replacement. The journal's observed/checked version guards never get a chance to protect the saved field; deletion is persisted as a tombstone.

**Reproduced through the actual Sort loader:** load v2 with thumbnail `new-upload`, confirm rating 5, then invalidate and download v1 with `old-upload` and rating 1. The returned rating is **1** instead of **5**. Source verification is temporarily unavailable in this test, and the receipt has already been removed, so subsequent reconciliation cannot protect it. This is an older payload in a current read, not an old in-flight request overwriting the cache.

Suggested correction: order generation observations using the actual payload/source revision and persist the generation's revision with its receipt. Reject an older generation observation before it can update the known generation or delete fields. Keep a separate regression for a genuinely newer replacement upload, which should still release the old receipt.

## 3. [P2] A matching older snapshot cancels reconciliation of a newer conflicting publication

Location: `frontend/src/services/pendingEdits.ts:125–127`, followed by source-response rejection at `:240–241`.

Any matching versioned row clears pendingProjection, even when the row's version is older than both the acknowledged publication and the pending conflicting publication. For example, acknowledge Alice at v2, observe Bob at v3 and start the required source check, then receive Alice from v1 in another outstanding page/search read. The v1 match clears the v3 conflict. When the valid source response returns Bob, acceptSourceEdits rejects it because pendingProjection no longer matches the request evidence.

The v2 observedVersion keeps `hasPendingEdits()` false, so the lost reconciliation does not itself keep polling alive. A further v3 read can reactivate the conflict, but the current publication refresh has failed to show the newer source value. Separate People page/search calls can produce this ordering despite the local blob loader's request-generation guard.

**Reproduced:** after the exact v2/v3/v1 sequence and a source response containing Bob, the overlay returns **Alice** instead of **Bob**.

Suggested correction: only a matching publication at least as new as the pending conflict may discharge it. Older matches must leave the pending request evidence intact. Gate acknowledgement, conflict creation, and conflict clearing by the same monotonic version rule.

## 4. [P2] The enrichment cap hides a matching recent rename from People search

Location: `frontend/src/mockups/prototype/store.tsx:168–174`.

The pending-ID list is truncated to its first 100 entries before considering the search query. The query is applied only after those selected IDs have been fetched. With 101 recent renames not yet reflected in the People projection, a search that matches only the 101st renamed person never looks that person up. Repeating the search selects the same first 100 IDs; there is no rotation or query-based prioritization.

**Reproduced through StoreProvider:** persist names New Name 0 through New Name 100, keep the server's search index stale, and search for New Name 100. Exact ID lookups are available, but the UI returns **[]** instead of person `pending-100`. This failure occurs before the backend rebuild, precisely when the journal is intended to keep the user's renamed person discoverable.

Suggested correction: filter/prioritize pending names against the active query before applying the lookup budget, and use fair pagination/rotation for other first-page enrichment. A request budget should not make a known matching pending entity unreachable.

## Reproduction artifacts

The assertions express required behavior and intentionally fail against the current implementation. They are retained outside normal test discovery:

- `docs/audits/all-uncommitted-2026-10-22.frontend.repro.test.ts`: copy to `frontend/src/services/finalAudit.test.ts` and run that file with the frontend Vitest runner. Three tests fail at the expected assertions. Remove the temporary copy afterward.
- `docs/audits/all-uncommitted-2026-10-22.store.repro.tsx`: append to a temporary copy of `frontend/src/mockups/prototype/store.gallery.test.tsx` in the same directory and run it with Vitest. The existing 20 tests pass; the appended search test fails. Remove the temporary copy afterward.

The unversioned-value reproduction explicitly reloads persisted journal state and advances time by ten minutes. The generation reproduction exercises downloaded payload versions through the real loader. The older-match case exercises the journal's request-token/projection-evidence contract. The search reproduction uses the actual StoreProvider with controlled API responses.

## Baseline validation and limits

- **115 existing focused frontend tests passed** across ten files, including all six previous remediation groups, the index caches, store, upload refreshes, and push lifecycle.
- **148 backend tests passed** across People coverage, reconciliation, CSP, events, role registration, labels, jobs, and the maintenance worker.
- TypeScript type checking, frontend entrypoint shell syntax, and `git diff --check` passed.
- Four additional adversarial checks failed as detailed above; those failures are not included in the passing baseline counts.
- No additional backend or deployment blocker was identified in this pass. That is a code-review result, not verification of live Azure authorization or message delivery.
- No production build/Bicep compilation was rerun here; both were previously checked, and this audit did not edit implementation files. No live Web PubSub connection, deployment, commit, or external message was performed.

The release assessment should include these new findings alongside the passing earlier regressions. The persistent-receipt strategy needs reliable revision ordering and a progress rule for unversioned data; the current value fingerprints do not fully supply those guarantees.


## Remediation — 2026-10-22

All four findings are fixed in the working tree:

1. Fresh request token sets carry distinct read identities and request ordering. A later unversioned page at the same formerly stale value demands a new bounded source check, including after reload. Cached reads opt out of fresh identities. Checks begun against superseded evidence cannot update the receipt.
2. Thumbnail generation observations carry the actual payload revision. Receipts persist that revision, inherit it during later edits, and restore it from IndexedDB snapshots. Older or equal-revision conflicting thumbnails cannot release saved edits; a provably newer replacement still can. Receipts without an ordered baseline remain conservative.
3. A persisted publication high-water mark governs acknowledgement, conflict creation, and clearing. Older matches and conflicts leave newer evidence intact across reload. Source checks also capture the publication revision; matching newer publications invalidate obsolete source responses. Pending conflicts keep recovery polling active until resolved.
4. People enrichment filters pending names by query before the 100-ID budget, rotates broad reads, deduplicates results, and retains each extra response's own payload revision. Search removes rows whose pending rename no longer matches and retains the requested result limit. Extra rows still do not advance the server offset.

The original reproductions above describe the pre-fix implementation. Passing versions now live in `frontend/src/services/pendingEdits.ordering.test.ts` and `frontend/src/mockups/prototype/store.gallery.test.tsx`. Additional tests cover immediate repeated-value revalidation, passive cache reads, superseded source responses, old conflicting publications, reload persistence, genuine replacement uploads, IndexedDB generation capture, fair enrichment rotation, query filtering, result limits, and the lookup budget. Existing generation fixtures now provide real ordered payload revisions rather than unversioned replacement assertions.

Validation: **127 focused frontend tests across 11 files and 148 backend tests passed**. TypeScript checking, the production frontend build, frontend entrypoint shell syntax, and `git diff --check` passed. The build retains existing dependency/WASM/chunk-size warnings. No deployment, live Web PubSub check, staging, or commit was performed.
