# UI index consistency audit — 2026-10-22

Original audit scope: the uncommitted pending-edit journal, its photo/person/album consumers, index-ready Web PubSub adapter, and deployment wiring. This is a local code audit, not verification of a deployed release. Existing backend indexing/publication findings remain separate.

**Original audit recommendation (before remediation): fix the P1 findings before deploying this change.** The consistency buffer also needs revision-based acknowledgement and cross-tab reconciliation to meet the stated persistence guarantee.

## Findings

### 1. [P1] A queued mutation can execute in a different library

Location: `frontend/src/services/pendingEdits.ts:133–138`; caller `frontend/src/mockups/prototype/store.tsx:1201`; credential attachment `frontend/src/services/httpClient.ts:24–32`.

The queue keys contain the original scope, but the deferred save callback does not capture or validate that scope. HTTP authentication is read from current localStorage when the callback eventually executes. Queue two ratings for the same filename in library A, switch to B in another tab while the first request is pending, then release the first request. The second request sends A's filename and rating with B's bearer token. If the same filename exists in B, it can change B's photo. The post-response scope check occurs after the mutation, so it cannot prevent this.

Reproduction used the real HTTP client's request interceptor and a controlled Axios adapter: expected `Bearer library-A-token`, received `Bearer library-B-token`. This is a confused application scope, not a bypass of backend authorization: it writes into another library the new session can access.

Fix: bind each mutation to its original authenticated context, enforce the context immediately before dispatch and on retries, and cancel or retain queued work explicitly when that context changes. Do not silently replay old work using the current session.

### 2. [P1] Production startup removes Web PubSub from CSP

Location: `frontend/docker-entrypoint.sh:82–85`; production API URL supplied in `deploy/resources.bicep:1375`.

The default `CONNECT_SRC` includes `wss://*.webpubsub.azure.com`, but the nonempty `API_BASE_URL` branch replaces the whole value without that origin. Production supplies this API URL. The generated security policy therefore blocks `new WebSocket(result.url)` even when push infrastructure and permissions are configured correctly. Static `frontend/csp.conf` does not repair the runtime replacement.

Evaluating the actual assignment block with `API_BASE_URL=https://production-api.example` produces a connect-src without the Web PubSub origin. Push will fail and polling remains the fallback; immediate ready notifications do not work under the normal deployment configuration.

Fix: preserve the WebSocket origin in both paths, preferably compose one common allowlist and append deployment-specific origins. Add an entrypoint assertion with a nonempty API URL.

### 3. [P2] Cross-tab storage updates can erase pending and confirmed edits

Location: `frontend/src/services/pendingEdits.ts:27–41`, `57–64`, `161–163`.

Two distinct races reproduce:

- A local edit is in flight when another tab writes the journal. The storage listener deletes the entire in-memory journal, including the unsaved fields. Later confirmation only mutates the detached field references and persists the rehydrated journal. The successful local save is absent from the buffer; stale reads can show its old value again.
- Two tabs begin edits to different photos before either storage event is delivered. Each tab serializes its entire cached journal. The second confirmed save overwrites the first tab's saved receipt in localStorage. Reload loses the first edit's protection.

Both cases returned rating 1 where the successfully saved local rating was 5. This directly violates persistence across navigation/reload during rebuilds.

Fix: retain in-flight entries separately, reconcile storage messages by entity/field identity, and use transactional persistence or independent mutation records rather than replacing one shared journal document. A read/merge/write of the document alone still has a race between tabs.

### 4. [P2] An intermediate value skipped by indexing remains authoritative forever

Location: `frontend/src/services/pendingEdits.ts:89–100`.

Releasing a receipt on a differing newer snapshot requires an `observedVersion`, which is set only after a snapshot contains the exact saved value. Save person name Alice in this browser; another client saves Bob before rebuilding. Every subsequent snapshot contains Bob, never Alice. The journal overrides Bob with Alice indefinitely, including after reload, and `hasPendingEdits()` stays true. Neither push nor repeated refetches can resolve this.

Reproduction supplied Bob in versions v2 and v3 after confirming Alice. Both reads still displayed Alice. Coalesced index builds make this a legitimate sequence.

Fix: return an authoritative mutation/entity revision from writes and publish the corresponding applied revision with index records or an adequate projection watermark. Once the projection covers the local mutation, allow a newer source revision to supersede it even when that intermediate value was never published. A timeout alone cannot establish correctness.

### 5. [P2] Server-paged photo reads have no acknowledgement path

Location: `frontend/src/services/localSortIndex.ts:231–233`; `frontend/src/mockups/prototype/store.tsx:77–78`; backend threshold `backend/routes/photos.py:1104–1105` (default 200,000 rows).

Photo acknowledgements occur on downloaded Sort snapshots. Libraries above the client index limit never download that snapshot. Their server photo rows pass through `mapPhoto`, which overlays pending edits without a version or read-token set. Even a page containing the matching committed value cannot mark the edit observed. Consequently ordinary ratings/likes/rotations remain pending indefinitely for these libraries and can keep masking later edits. Idle job polling also schedules repeated refreshes every six seconds until its 20-minute bound whenever an unacknowledged edit remains.

This finding is established by tracing the large-library branch and all photo `applyPendingEdits` call sites; a full large-library browser test was not run. The analogous no-version source fallbacks and cached-IDB paths also need explicit acknowledgement coverage.

Fix: expose compatible revision/acknowledgement information on paged/source responses and consume it under the same request-start safeguards as blob reads.

## Validation and limits

- Four adversarial frontend tests reached their intended assertions and failed with the outcomes recorded above. They cover finding 1, both races in finding 3, and finding 4.
- All 87 existing checks in seven focused frontend files passed during this audit. Passing coverage does not exercise the reproduced sequences.
- All 20 backend event and application-role registration checks passed during this audit.
- `git diff --check` passed.
- CSP assignment reproduction confirmed the missing WebSocket origin with a production-style API URL.
- No live Web PubSub connection or Azure deployment was attempted.
- Application implementation was left unchanged. Audit report and reproductions are the only additions made by this audit.

The intentionally failing reproductions are stored outside the ordinary test suite in `docs/audits/ui-index-consistency-2026-10-22.repro.test.ts`. Run from the repository root:

```bash
cp docs/audits/ui-index-consistency-2026-10-22.repro.test.ts frontend/src/services/pendingEdits.audit.test.ts
(cd frontend && ./node_modules/.bin/vitest run src/services/pendingEdits.audit.test.ts)
rm frontend/src/services/pendingEdits.audit.test.ts
```

They currently expect the required safe behavior; the four failures are evidence of defects rather than a passing regression suite. The authentication reproduction accepts either binding to the original session or cancellation as a design choice, but its current assertion checks binding specifically.


## Release remediation — implemented locally

All four defect groups and the separate server-paged acknowledgement finding have been addressed. The findings above and their intentionally failing reproduction file describe the pre-fix implementation; the current acceptance tests exercise the corrected contract, including cancellation rather than replaying queued work under an old session.

| Finding | Correction | Regression coverage |
| --- | --- | --- |
| Queued save changes library | Cancel the queue item when its original account/library differs; pin HTTP scope through dispatch, token acquisition, retries, and responses; prevent old-library cache results and rename callbacks from updating the new session. | `pendingEdits.release.test.ts`, `reconcilePendingEdits.test.ts` |
| Production CSP blocks push | Append the configured API origin to the common connect-src allowlist, preserving Web PubSub. | `backend/tests/test_frontend_csp.py`, both API configurations |
| Cross-tab receipt loss | Independent v2 field records, unchanged-write suppression, in-flight-preserving event merges, and rollback to the latest received receipt. Legacy confirmed receipts and deletion tombstones survive reload. | Concurrent-tab, stale-tab, storage-event, rollback, migration cases in `pendingEdits.release.test.ts` |
| Index skips the intermediate value | Batch exact authoritative source reads after projection reads; update only fields whose request tokens still match; retain canonical receipts over stale projections. The second People name buffer now defers to verified source values. | Journal, reconciliation service, and StoreProvider People-state tests |
| Paged photo reads cannot acknowledge | Apply the same source check to paged fallbacks and cached index paths; source-confirmed receipts no longer keep idle polling alive. | Server-paged integration in `reconcilePendingEdits.test.ts` |

The new authenticated endpoint is `/api/tools/pending-edits/reconcile`. It allows at most 50 entities, only the supported editable fields, at most eight concurrent point reads, and uses the authenticated library partition. Responses are non-cacheable. Missing rows and temporary storage failures are distinguished. Source verification establishes current persisted source values; it does not claim that an index rebuild has finished. Receipts continue to protect the UI while projections lag.

Final local validation:

- **105 frontend tests passed** in nine focused files, including provider-level rendering, all added adversarial cases, generation invalidation, push reconnects, and upload refresh behavior.
- **146 backend tests passed** across source reconciliation/CSP/events/role registration (41) and existing People/labels/job-status/index-maintenance coverage (105).
- TypeScript type checking, the production frontend build, shell syntax, and `git diff --check` passed.
- The build still reports dependency/browser-externalization, WASM resolution, and chunk-size warnings. They do not fail the build.

Release order: deploy the backend endpoint before the frontend. Push activation still requires the optional infrastructure setting `enableIndexPush=true`. No production deployment or live Web PubSub transport validation was performed in this remediation.
