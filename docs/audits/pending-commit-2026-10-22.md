# Pending commit audit — 2026-10-22

**Original audit: six findings, including two P1 release blockers. The remediation below is now implemented locally.** This audit reviews the current unstaged production diff and added services/tests, including the previous remediation. No changes are staged. Application implementation was left unchanged by this audit. The findings below supplement the earlier audits; passing the earlier regression suite did not cover these sequences.

## 1. [P1] A partial People rebuild clears unrelated source changes

Location: `backend/storage_utils.py:7194–7201`.

The new `previous_manifest.get('dirty')` alternative admits any schema-compatible old snapshot as a base for an ID-only refresh. A dirty manifest does not establish that the supplied IDs cover every change since that snapshot. In particular, photo trash/restore updates face visibility and touches the People manifest without adding those people to the per-person dirty set (`backend/app.py:13567–13583`). If a label job for a different person runs against that dirty base, it refreshes only the label's person, retains the old visibility/count/cover for everyone else, and publishes a globally clean manifest. Subsequent readers have no dirty signal to repair the omitted change.

**Reproduced:** build p1 and p2, hide p2's only face and dirty the manifest, then refresh only p1's label. The new snapshot reports p2 faceCount **1**, while a full source build reports **0**; the published manifest is **clean**. This occurs without simultaneous blob writes. It is a separate regression from the earlier publication races.

Fix: require proof that the affected-ID set covers the complete dirty interval. Track dirty revisions/IDs for every producer (including visibility changes), or rebuild fully when coverage cannot be established. An arbitrary dirty bit is insufficient.

## 2. [P1] Source verification blocks an otherwise usable cached UI

Location: `frontend/src/services/localSortIndex.ts:265–269`; equivalent cache branches in `localPeopleIndex.ts` and `localAlbumsIndex.ts`; transport in `reconcilePendingEdits.ts:29`.

A warm in-memory index used to return immediately. The new cache path waits for source verification before returning any rows. That verification has no separate deadline or abort signal and uses the tools HTTP client's default **600,000 ms timeout per attempt**, with cold-start retries (`httpClient.ts:18`, `apiClient.ts:84`). A stalled or unavailable tools service can therefore block gallery/People/Albums navigation even though both the cached index and saved overlays are already available locally. Catching failures preserves receipts only after the network call finally fails.

**Reproduced:** preload a usable index, save one rating, stall source verification, then request the warm index. It remains unresolved after five seconds of simulated time. The ten-minute timeout is established by code inspection; the test does not wait ten real minutes or exercise a live outage.

Fix: return cached rows plus receipts immediately and reconcile asynchronously with a refresh notification, or give verification a short bounded deadline and retain overlays on timeout. Do not make source revalidation a prerequisite for rendering a valid cached view.

## 3. [P2] A storage event breaks the chain of overlapping local edits

Location: `frontend/src/services/pendingEdits.ts:246–248`.

When the current field is unsaved, the storage listener replaces `current.previous` with the incoming receipt. That discards any older local in-flight field linked there. For local saves 3 then 5, receive another tab's receipt 1, let save 3 succeed and save 5 fail. Confirmation of 3 mutates a now-detached field, while rollback of 5 restores 1. The successful save loses both its display protection and persisted receipt.

**Reproduced:** expected rating **3**, actual **1**. The previously added single-in-flight storage test passes because it does not contain the intermediate local request that this event detaches.

Fix: preserve the entire local in-flight chain and reconcile the incoming confirmed ancestor without replacing pending predecessors. Confirmation must remain connected to the active chain even when another tab updates the receipt.

## 4. [P2] An in-flight album response can undo a successful rename

Location: `frontend/src/mockups/prototype/store.tsx:1595`; raw accessor at `:617`.

`openAlbum` merges the returned album into React state without applying pending edits. Start an album-open request, rename the album and receive success, then let the older open response return its old name. It overwrites the optimistic/saved name. `albumById` reads that state directly, so the album title regresses even though the journal still contains the confirmed rename. The album list loaders apply overlays, but this separate response path bypasses them.

**Reproduced through StoreProvider:** title is New after successful rename, then **Old** after resolving the earlier request. The original request-generation guard only compares album-open requests; it does not notice the intervening rename.

Fix: apply the album receipt when merging detail responses and reading the album, or invalidate outstanding detail responses when a rename occurs.

## 5. [P2] Bulk confirmations perform quadratic work on the browser main thread

Location: `frontend/src/mockups/prototype/store.tsx:1211`; `frontend/src/services/pendingEdits.ts:55–79`.

A bulk rating creates N journal entries, then confirms each entry separately. Each confirmation walks and serializes all N fields in the scope, even though unchanged writes are eventually skipped. This makes confirmation O(N²), before adding historical receipts already retained in the journal. Rollback uses the same full-scope persistence path once per failed row.

**Reproduced:** confirming an 80-photo batch invoked JSON.stringify **6,480 times**. The count grows quadratically; extrapolated bulk/library scale was not benchmarked as a live UI freeze. This is synchronous work and cannot yield for rendering or input while the completion callback runs.

Fix: batch confirmation/rollback and flush once, or persist only fields changed by the current operation. Skip unchanged work before serialization rather than merely skipping setItem afterward.

## 6. [P2] Every refresh re-reads the complete history of edited entities

Location: `frontend/src/services/pendingEdits.ts:207–212`; `frontend/src/services/reconcilePendingEdits.ts:26–33`.

Receipts are intentionally retained after both source and projection acknowledgement. `sourceEditRequests` selects every saved receipt, including those with sourceConfirmed and observedVersion, and reconciliation submits all of them in sequential batches. Every fresh index/page read repeats that history, and cached reads repeat it once the one-second coalescing window expires. The 50-entity limit bounds each request, not the total work of one UI refresh.

**Reproduced:** 51 fully source- and projection-acknowledged photos, no pending edits, no new mutation: the next refresh still sends **two requests covering all 51 entities**. By direct arithmetic, 10,000 retained entities require 200 sequential API calls per such refresh; this scale was not exercised against Azure. Historical person IDs also feed the first-page enrichment path, adding further work after many renames.

Fix: separate unresolved edit buffers from long-lived stale-replica receipts. Reconcile only unresolved fields or records whose incoming projection demonstrably conflicts, using per-entity revisions/watermarks and a bounded refresh work budget. Retaining protection need not require re-reading the complete edit history on every navigation.

## Reproductions and validation

Six adversarial checks reached their intended assertions and failed: four service-level frontend checks, one StoreProvider check, and one backend People projection check. The existing focused suites were rerun separately; their results are recorded below.

Reproduction artifacts (intentionally outside normal test discovery):

- `pending-commit-2026-10-22.frontend.repro.test.ts` — copy to `frontend/src/services/pendingCommit.audit.test.ts`, run that file with Vitest, then remove the temporary copy.
- `pending-commit-2026-10-22.store.repro.tsx` — append to a temporary copy of `frontend/src/mockups/prototype/store.gallery.test.tsx` in that same directory, then run that copy with Vitest and remove it. The original fixture tests pass; the appended album test fails.
- `pending-commit-2026-10-22.backend.repro.py` — copy to `backend/tests/test_pending_commit_audit.py`, run with pytest, then remove the temporary copy. It reuses the existing People fixture and includes a full-source control build.

No fixes, commits, deployments, messages to external services, or live Azure transport validation were performed. The old audit reproduction files remain historical artifacts; these new checks exercise the current pending implementation.

Baseline validation during this audit:

- **105 frontend tests passed** in the nine existing focused journal/reconciliation/index/store/provider/push test files.
- **146 backend tests passed** across source reconciliation, CSP, events, role registration, People, labels, job status, and the index-maintenance worker.
- TypeScript type checking, frontend entrypoint shell syntax, and `git diff --check` passed.
- No new production build or Bicep compilation was run in this audit; those succeeded in the preceding remediation turn, and application files were not edited by this audit.


## Remediation — all six findings covered by passing regression tests

The sections above record the pre-fix audit and its evidence. The current implementation addresses each finding:

1. **People coverage:** both incremental paths require a clean, matching base. A dirty or unproven base takes the full source rebuild, preserving unrelated visibility changes. The tradeoff is extra work for dirty libraries; the clean-base point-read path remains tested.
2. **Cached UI availability:** index/page reads render their existing rows plus receipts immediately. Source checks run in the background, use a three-second deadline and AbortController, and disable the general HTTP retry loop. Late responses are ignored. Canonical changes update loaded StoreProvider collections through a scoped subscription.
3. **Rollback chains:** storage events attach incoming confirmed state below all local in-flight predecessors. They no longer detach a successful predecessor that a newer failure must restore.
4. **Album names:** detail-response merges and the album accessor apply the name receipt. The original stale-open/rename reproduction is now part of the StoreProvider suite.
5. **Bulk persistence:** confirmation, rollback, publication acknowledgement, source acknowledgement, and generation changes persist only their touched fields. The 80- and 1,000-photo cases have linear serialization bounds, and changing one field does not serialize 1,000 historical siblings.
6. **Historical reads:** acknowledged receipts are passive unless a fresh incoming row conflicts. Actual payload versions survive IndexedDB and memory caching separately from manifest validators. Older replicas do not reactivate fully acknowledged edits. Each source pass is capped at 100 entities (two API batches), unresolved work rotates after a failed pass, and unresolved People first-page enrichment is also capped at 100 IDs.

Passing release checks for this remediation:

- **115 frontend tests** in ten focused files, including `pendingCommit.regression.test.ts`, the updated reconciliation service tests, StoreProvider, and the existing index/push/upload suites.
- **148 backend tests**, including `test_people_dirty_coverage.py`, the dirty-manifest full-build regression, and the clean-base targeted-build control.
- TypeScript checking, production frontend build, shell syntax, and diff whitespace checks passed.
- Existing dependency externalization/WASM resolution and bundle-size warnings remain nonfatal build warnings.

Application changes are still uncommitted and undeployed. Push activation and a live Web PubSub transport check remain deployment work; these tests use controlled transports. The historical reproduction artifacts stay outside normal discovery, while the current regression versions are in the ordinary test suites.
