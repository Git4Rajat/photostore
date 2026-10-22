# Deeper indexing audit — 22 October 2026

## Assessment

The dedicated-worker migration is not ready to enable unchanged. The first audit found a partial deployment, a legacy label scheduling race, and an Albums/Search dependency error. This pass found additional correctness and migration risks, including two potential data-loss races.

The highest priorities are immutable Search publication and removing unconditional source deletion from People index construction. Queue durability does not address either problem. Separately, long-running worker ownership must remain visible to the scaler.

This audit used an isolated snapshot of deployed commit `dc96b4ba678214bdbe183de3ef66eb7218ff1709`. The shared checkout has concurrent uncommitted fixes; those were preserved. Findings below identify inherited gaps versus changes introduced by the latest commit. No production mutation or infrastructure change was performed.

## Findings

| Priority | Finding | Applicability | Evidence |
| --- | --- | --- | --- |
| P1 | A losing Search publisher can overwrite a committed delta | Inherited; relevant during overlapping publishers/revisions | Deterministic reproduction, including real SQLite application and active publication guards |
| P1 | People index cleanup can delete a newly named source person | Inherited; both execution modes | Deterministic stale-snapshot reproduction |
| P1 | Visible-only scaling can remove a worker with an active long job | Dedicated template; legacy workers use the same scaling strategy | Configuration plus official scaler/platform semantics; inferred risk, not a reproduced live incident |
| P2 | Dirty People manifests defeat targeted incremental rebuilding | Deployed code; workspace contains a candidate fix | Reproduction against deployed commit |
| P2 | Missing filename lookup prevents first-name/removal synchronization from progressing | Latest commit; both modes | Repeated failure reproduction; no missing lookup in the live sample |
| P2 | Separate compaction drops Explore-summary refresh | Latest commit; dedicated mode | Compaction → Search reproduction |
| P2 | People/Sort browser caches accept obsolete results and temporarily return another library's rows | Deployed code; current workspace fixes pass the focused checks | Six failing tests against deployed commit; six passing against workspace |

The previous audit's three findings remain relevant. See [the initial audit](indexing-2026-10-22.md).

### 1. Search manifest CAS does not protect delta contents

Locations: `backend/search_db.py:798`, `:993`–`1021`; `backend/index_publication.py:58`–`76`.

Delta names depend only on library and sequence. Two publishers that read sequence zero both choose the same sequence-one blob. The following legal interleaving loses a committed update:

1. B reads the sequence-zero manifest and pauses.
2. A writes delta one containing a.jpg, advances the manifest, and returns success. The incremental caller can now acknowledge a.jpg's dirty record.
3. B writes its own delta one containing b.jpg with `overwrite=True`.
4. B's manifest CAS detects the changed sequence and refuses its commit. The shared blob has already been replaced.
5. A fresh reader applies sequence one and sees only b.jpg. Readers that already applied A's original bytes can disagree with fresh readers despite sharing the same manifest sequence.

The reproduction uses actual `row_record`, `publish_delta`, manifest CAS, gzip decoding, and SQLite delta application. A returns sequence one; B returns `None`; the reader contains b.jpg and not a.jpg. Both scoped work claims and publication guards are valid. The wrapper reads the blob ETag immediately before uploading, so B can successfully condition its overwrite on A's newly published ETag. It does not compare against a version captured before deriving the content.

The one-worker limit reduces concurrency during a stable revision but is not a library-wide storage guarantee: scopes have separate leases, and revisions can overlap. Earlier rollout logs already showed old and new cluster revisions processing index work concurrently. This establishes a reachable topology, not evidence that this particular corruption occurred live.

Recommended correction: make delta payloads immutable, with lineage and publication identity in their names and explicit references in the committed manifest. A losing publisher must never modify bytes referenced by a winning manifest. Include the same invariant when reviewing the mutable Search base and other legacy snapshot artifacts. Sequence alone and upload-time ETags are insufficient.

### 2. An index rebuild can delete authoritative naming work

Location: `backend/storage_utils.py:6981`–`6993`.

People construction reads source people, then later resolves faces and removes empty unnamed clusters. The removal uses an unconditional `delete_entity` based on the earlier person snapshot. If another request names that person or adds membership after the scan, the index builder can delete the updated person, including its `labelPending` token.

The reproduction captures an unnamed empty person, changes the stored person to Alice with a pending label token, then lets the builder consume its original snapshot. The source person is deleted. Preserving named clusters in the normal branch does not protect against this stale-read race.

Recommended correction: keep index generation read-only. Perform cleanup through a separate mutation path that rereads the current person, validates membership and named status, and deletes only with that source ETag. Preserve source-version information if cleanup candidates come from a projected scan. A lease protecting index work does not serialize the human naming API.

### 3. Renewing a queue message does not keep it visible to the scaler

Locations: `deploy/resources.bicep:957`, `:1014`–`1042`; `backend/app.py:14985`–`14990`, `:15117`–`15142`, `:15589`–`15626`.

The dedicated template combines `minReplicas: 0`, `queueLengthStrategy: visibleonly`, and a 180-second termination grace period. The worker makes the claimed message invisible and renews its visibility timeout while processing. For a single long job with no other queue backlog or HTTP activity, the scaler sees zero work even though the job is still running.

KEDA documents that `visibleonly` counts visible messages using Peek. Container Apps documents a default 300-second cooldown before the last replica scales to zero. The resulting risk is scale-in during a live build, followed by forced termination if it outlasts shutdown grace. Queue redelivery and bounded retries can then repeatedly restart work without making an uninterrupted build finish. Exact timing depends on platform evaluation and activity; this is an inference from the configuration, not an observed dedicated-worker shutdown. [KEDA queue scaler documentation](https://github.com/kedacore/keda-docs/blob/main/content/docs/2.19/scalers/azure-storage-queue.md), [Azure Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app).

Recommended correction: include in-flight work in the scale signal, or keep a worker replica available while that work is owned. Counting all queue messages also counts delayed messages, so evaluate the cost of deferred dedup wakeups explicitly. Validate one long job with no browser traffic before accepting the rollout. A renewed message receipt protects against duplicate delivery; it is not a replica keepalive.

### 4. Targeted People updates usually reject their own base

Locations: `backend/storage_utils.py:6723`–`6741`, `:7194`–`7225` in the deployed snapshot.

Dirty marking writes a fresh edit-time `sourceVersion` into the People manifest. The data blob still carries its last published version. The targeted-ID path requires those versions to match, so it rejects the normal dirty state and falls back toward a full people/face scan. This defeats the intended small-rename path.

The diagnostic supplies one affected ID, a schema-compatible published snapshot, and a dirty edit-version manifest. The targeted builder is never called; the full builder is called. Earlier live logs also showed a People build taking about 30 seconds with extensive table reads, but those logs alone do not prove every rename uses that path.

The shared checkout already contains a candidate change accepting a dirty manifest with a schema-compatible base. That change was not part of the deployed image and was not authored by this audit. Its normal regression test is outside this pass's pinned baseline. Ensure it retains the complete outstanding dirty-ID set before allowing a merge; accepting a base is safe only when intervening changes remain represented.

### 5. Strict membership synchronization has an unrepaired prerequisite

Locations: `backend/app.py:10797`–`10820`, `:7373`–`7380`.

First naming and removal now use strict filename-to-face lookups. A missing, dirty, or legacy-schema lookup returns `None`, and strict mode raises before updating photo membership. That is appropriately conservative about an unknown face set, but retrying the same label job does not repair the lookup. The person stays pending and repeated attempts can exhaust retries. This replaces the previous fallback with a new migration prerequisite.

The reproduction fails repeatedly with an otherwise readable person, face, and photo because the filename lookup is absent. Live sampling found all 100 sampled files had complete lookup rows, so this is a demonstrated compatibility gap rather than an established current incident.

Recommended correction: explicitly repair/reconcile affected filename indexes, or schedule that bounded prerequisite before retrying membership synchronization. Distinguish missing coverage from an actively leased face writer. Do not restore a silent empty-result fallback. The existing explicit face-index repair tool is a recovery option, but ordinary label retry and catalog reconciliation do not invoke it.

### 6. Compaction no longer refreshes Explore in dedicated mode

Locations: `backend/app.py:10919`–`10940`, `:11037`–`11055`; `backend/routes/explore.py:27`–`32`.

The legacy inline compaction path sets a local `compacted` flag and refreshes summaries after Search catch-up. The new separate `compact` job compacts and schedules `search`, but passes no summary-refresh obligation. That new Search job starts with `compacted=False`. Neither job refreshes the summaries.

The reproduction runs the actual compact and subsequent Search job branches and verifies no summary refresh occurs. Explore serves its existing precomputed artifact without checking its source version, so it can remain stale after both jobs report success. Timeline's normal database-backed route is less exposed, but its stored fallback is also not refreshed through this path.

Recommended correction: persist a summary-refresh dependency across the compaction/catch-up handoff, or schedule a separate summary scope after catch-up. Do not claim Explore freshness merely because Search succeeded. This obligation should also be explicit for ordinary tag/location deltas if the product expects Explore to reflect them promptly rather than only after compaction.

### 7. Browser caches can undo a successful server refresh

Locations: deployed `frontend/src/services/localPeopleIndex.ts` and `localSortIndex.ts`, around `getLocal*Index` and invalidation.

Both caches permit an old request to complete after invalidation and overwrite newer cached data. An old completion can also clear the newer in-flight marker. During a library switch, changing the cached key without clearing the old cached rows permits a second caller to receive the previous library's rows while the new fetch runs.

The existing in-progress `localIndexFreshness.test.ts` was copied into the isolated deployed snapshot. All six checks fail there: three cases each for People and Sort. The same six pass in the shared checkout with its current fixes. This is a frontend cache-isolation/freshness issue; no bypass of server authorization or cross-user exposure was demonstrated.

The workspace also contains changes to refresh on index completion and prioritize index jobs in the bounded status response. Those address an adjacent gap: durable server completion is not useful to an already cached browser unless the completion is delivered and invalidates the relevant view. They still need their own full review and coordinated deployment.

## Recovery and scale boundaries that remain explicit limitations

- A source outbox token is not an autonomous delivery mechanism. Source commit followed by failure before ledger creation still requires explicit source reconciliation. The worker's periodic recovery queries the ledger, not every source table. With zero replicas and no visible message, it also has no independent activation signal. The migration document acknowledges this limitation.
- Source reconciliation returns `no_db` before relaying metadata outboxes if Search has no usable base. The worker schedules bootstrap and retries. This is a dependency, not proof of lost source data; the diagnostic confirms it must not be treated as an independent outbox relay.
- A failed send aborts one recovery pass before later libraries are notified. The failed row's wake reservation normally allows later loops to proceed, so the reproduction does **not** establish permanent fleet starvation. Per-row error isolation would improve recovery latency.
- Whole-file People/Albums/Sort/Access publication and a single serial consumer remain. Coalesced queue wakeups do not remove per-edit ledger/dirty writes or whole-snapshot merge cost. This audit does not certify the multi-user or million-photo throughput targets.

## Live evidence and limits

Read-only checks around 2026-10-21 22:18–22:21 UTC reconfirmed the indexer image is `dc96b4b…` with only the API container and no dedicated queue configuration. The original rollout finding therefore still applies.

A server-filtered query found zero `labelPending` rows for the owner library, with no continuation marker. A separate sample of 100 face rows covered 100 filenames; all 100 had complete filename lookup rows. Neither result proves every index is current or rules out historical races.

The signed-in Azure principal could read the relevant tables but was denied blob data access. Consequently this pass could not compare live source rows with downloaded index contents, determine whether replicas had divergent Search deltas, or certify source/index parity. No credentials or permissions were changed to work around that limitation.

## Reproducibility and validation

- **402 existing focused backend tests passed** against the isolated deployed commit.
- **7 diagnostic backend tests passed**, deliberately asserting the defective or limiting behaviors above. They do not represent seven correctness successes.
- **6 frontend freshness assertions failed** against deployed source and **6 passed** against the current shared checkout.
- The prior two diagnostic reproductions remain in the initial audit artifact.

Backend diagnostics: [reproduction file](indexing-2026-10-22-deep-reproductions.py). Frontend checks: [freshness test copy](indexing-2026-10-22-frontend-freshness.test.ts).

Run backend diagnostics against the pinned source, rather than a checkout containing fixes:

```sh
audit_dir=$(mktemp -d /private/tmp/photostore-index-audit.XXXXXX)
git archive dc96b4ba678214bdbe183de3ef66eb7218ff1709 backend | tar -x -C "$audit_dir"
cp docs/audits/indexing-2026-10-22-deep-reproductions.py "$audit_dir/backend/tests/test_deep_index_audit.py"
.venv-photostore-tools/bin/python -m pytest "$audit_dir/backend/tests/test_deep_index_audit.py" -q
```

## Recommended implementation order

1. Protect source people from stale cleanup deletion and make committed Search artifacts immutable. Add adversarial interleaving tests that expect preservation, reversing the diagnostic assertions.
2. Correct worker scale-down behavior and validate a long, isolated job. Establish a safe single-writer rollout strategy for the remaining mutable legacy artifacts.
3. Preserve dependency obligations: Search before album-cover publication, filename-index repair before membership synchronization, and Explore refresh after compaction/catch-up.
4. Review and ship the existing incremental People and browser freshness fixes. Validate the bounded job-status delivery path under upload load.
5. Deploy producer and worker configuration together, then verify live parity and recovery using a controlled mutation. No live mutation or deployment was performed during this audit.
