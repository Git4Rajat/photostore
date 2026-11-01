# Extending incremental index maintenance

Assessment date: 1 November 2026. Scope: repository inspection and local tests;
no runtime or deployment changes.

## Decision

Keep the existing builders for large libraries, bulk changes, bootstrap, repair
and incompatible schema/model upgrades. Add incremental coverage alongside them.
Small changes in a large library can also qualify: select the maintenance path
using the affected entities and the cost of the affected artifact, rather than
classifying the entire library as incremental or full-build only.

Incremental maintenance is feasible for all published index families, with
different amounts of work. It is already implemented for several. Extending
the inline scope allowlist alone would not provide the missing capabilities.

Two distinct capabilities matter:

1. **Incremental computation:** fetch/recompute only affected source entities,
   then merge into a snapshot. A whole snapshot download/upload can remain.
2. **Incremental publication:** publish upserts/deletions separately from the
   base, and let readers apply them. This bounds routine publication by change
   size, subject to cold-reader base downloads and periodic compaction.

The first is the smaller extension. The second is needed where a minor edit
must remain cheap even when the published artifact itself is large. Neither
requires retiring the optimized full builders.

## Current coverage and extension options

| Index / projection | Current behavior | Additional work for incremental coverage |
| --- | --- | --- |
| Search database | Base plus published upsert/delete deltas, local SQLite application, manifest CAS and separate compaction. | Retain this path. Audit mutation coverage and dependency ordering; use its protocol as a reference, without assuming every artifact can reuse its payload. |
| Legacy lexical snapshot | Dirty-filename point reads and snapshot merge; full serialization/publication. Refresh also writes listing and a Search database snapshot. | Preserve for compatibility. If actively required by consumers, support deltas for those consumers; do not invoke this legacy refresher as the small Search update path because its side effects publish a full database. |
| Listing snapshot | Derived from lexical snapshots or a streaming full-library pass. No independent delta reader/writer. | Derive listing upserts/deletes from the same committed photo changes, using its own field projection and publication coverage. Adapt its readers if adding base-plus-delta storage. |
| Sort | Changed-photo point reads, previous-file streaming merge; full gzip artifact rewritten. Existing cost crossover chooses a scan for bulk changes. | Incremental computation already exists. Add delta publication/read support only if whole-file transfer is material. Preserve the disk-backed builder and crossover. |
| Access | Same streamed merge infrastructure as Sort, with its own dirty set and row projection. | Same publication extension as Sort; preserve its distinct visibility rules, including required trashed-photo access. |
| People | Targeted person recomputation for up to 100 affected IDs when the base matches; merges and republishes the whole People snapshot. Timestamp-based and full fallbacks also exist. | Audit every person/face mutation for old and new affected owners. Bound affected face reads as well as person count. Preserve ordering and generated unnamed labels in any delta reader. |
| Albums | Targeted album recomputation for up to 100 affected IDs; merges and republishes the whole Albums snapshot. Cover selection uses the Search database. | Ensure membership, rating/visibility and share changes identify affected albums. Cover calculations must use the required Search changes. Add deltas only if the album artifact warrants it. |
| People embeddings | Scans people and face ownership; rebuilds the matrix and metadata. | Add a separate dirty-person stream for this consumer. Recompute affected representatives/metadata and validate their active face ownership. Merge only compatible dimensions/schema; preserve the existing full path otherwise. |
| Tag embeddings | Scans effective tags across photos, encodes the complete distinct vocabulary, republishes the matrix. Requires the real Vision encoder. | Persist each photo's previous effective tag membership and idempotently update tag usage counts (or equivalent membership records). Encode newly introduced tags only; remove a tag only after its final reference disappears. Pin model/version/dimension. Keep encoding with Vision. |
| Published photo vectors | Active refresher deliberately performs a disk-backed full build. An older in-memory merge helper remains, but is not the active refresh path. | Add reliable per-photo tracking and compatible row upserts/deletions. Use bounded segments/deltas or a disk-backed merge; do not reactivate the whole-matrix RAM merge for large libraries. Retain on-demand behavior unless consumers require more. |
| Upload hash download | Full streamed snapshot of the hash table; deletion epoch invalidates stale downloads. | Track hash upserts/deletes durably and merge the snapshot for small changes, or add client delta support. Preserve deletion safety so removed hashes cannot cause incorrect upload skips. |
| Table lookup indexes | Hash/filename ownership, trash entries, public album tokens and face-filename lookup already have entity-level maintenance paths. | Audit write/delete/retry coverage separately. These are not missing full-snapshot incremental builders; retain backfill/reconciliation facilities. |

This is coverage of the catalog artifacts and related lookup families inspected
here, not a claim that every source writer or every frontend consumer has been
audited. The internal clustering FAISS index/checkpoints are algorithm runtime
state, separate from published catalog maintenance.

## Existing inline facility

`backend/inline_index_merge.py` currently allows People, Albums, Sort and Access
only, behind a default-off feature flag and the `core` role. A bounded local
queue attempts work while a durable maintenance message remains the backstop.
The same maintenance claim protects the scope against another worker.

Its current `INLINE_INDEX_MERGE_MAX_ROWS` default of 5,000 gates on the Search
manifest's photo count for every scope. That is a poor cost proxy for People
and Albums: their merge processes their own snapshots. The existing
`docs/benchmarks/inline-merge-index-size-scaling-20261031.md` records a local
test-double benchmark showing this distinction. Its suggested time budgets are
not cloud measurements and should not be treated as production thresholds.

For People/Albums, budget the artifact bytes/rows and affected source work.
For Sort/Access, whole-file merge cost still grows with the photo artifact.
Unknown size should route to the worker rather than be interpreted as zero.
Eligibility must be revalidated after acquiring the claim. An inline attempt
needs an explicit incremental-only mode: the current refreshers can fall back
to a full build after the advisory eligibility check. Expensive fallback should
stay with the durable worker.

Incremental and inline are separate choices. New embedding or hash incremental
paths can run on their existing worker owners first, without broadening what
the HTTP-serving process is allowed to build.

## Shared correctness requirements

- Record changes durably by stable entity identity. Retain deletion tombstones
  and both former/new memberships where needed. A dirty manifest alone cannot
  identify the records to recompute.
- Give each projection independent acknowledgement or explicit coverage. The
  People snapshot must not consume the only IDs needed by People embeddings;
  Search must not consume the only photo changes needed by Listing or tags.
- Reuse maintenance revisions, leases and conditional dirty acknowledgements.
  Publish data and activate the matching manifest before acknowledging work.
  Do not clear dirty work or report success after failed artifact publication.
  Audit each existing publisher's failure propagation before reusing it.
- Treat a confirmed missing/deleted source as deletion. Retry permission,
  timeout and other transient reads instead of deleting a previously valid row.
  The legacy lexical/vector merge helpers and album point-read loop include
  broad exception handling; copying them verbatim would copy this ambiguity.
- Validate base generation, schema and model compatibility. All writers to the
  same artifact must honor publication fencing, including builders, inline
  merges, compaction and recovery. Per-scope claims alone do not coordinate
  different scopes that publish a shared artifact.
- For delta publication, bind sequence and base generation, make replay
  idempotent, publish deletions explicitly and define reader catch-up and
  compaction/retention. Keep old-reader compatibility until every relevant
  backend and browser reader is migrated.
- Respect existing wave/foreground publication rules and owner boundaries.
  A small foreground edit must not accidentally drain held bulk upload work.
- A field/schema/model upgrade may affect all records even when triggered by
  one request. Bootstrap, incompatible bases and genuine bulk changes retain
  the current full-build path.

## Suggested implementation order

1. **Make the existing incremental contract explicit.** Add per-kind
   capability/result reporting (incremental, no-op, full build needed), retain
   durable fallback, correct inline cost eligibility, and audit mutation and
   acknowledgement coverage. Measure source reads, artifact bytes, memory,
   publication time and fallback reason.
2. **Add People-embedding incremental computation.** It has stable person IDs
   and a natural targeted source path. Include rename, merge/split, rejection,
   representative changes and deletion cases. A person with many faces still
   needs a work budget.
3. **Extend hash and tag tracking.** Hashes need deletion-safe delivery; tags
   need old/new photo membership, replay-safe usage accounting and Vision-owned
   encoding. These require source tracking, not just merge functions.
4. **Extend publication where measured costs justify it.** Start with large
   photo-level Sort/Access/Listing artifacts if whole-file transfer dominates.
   Keep small People/Albums snapshots as merged snapshots where cheaper. Add
   published vector deltas only if actual demand warrants the storage and
   reader work; preserve its memory-safe full builder throughout.

The rollout unit should be one index kind behind its own flag, using the same
source-to-row rules for full and incremental outputs. Compare both outputs at
the same captured source state before enabling writes. Equality checks must
account for deterministic ordering, generated labels, floating-point policy and
intentional timestamps.

Required validation includes create/update/delete/restore, repeated delivery,
mutation during publication, confirmed 404 versus transient failure, missing or
incompatible bases, and retry after data upload but before activation. Benchmark
a small library and a small change in a large library independently; retain
bulk-builder throughput measurements as a regression gate.

## Source anchors

- `backend/index_updates.py`: maintained scopes and inline allowlist.
- `backend/app.py`: `_run_index_build_job`, `_drain_incremental_search_db`,
  `dispatch_scoped_rebuild`, hash/token/trash producers.
- `backend/storage_utils.py`: `metadata_index_kinds`, `refresh_user_search_db_incremental`,
  `_refresh_rows_index_on_disk`, `_people_merge_eligible`,
  `_albums_merge_eligible`, the embedding refreshers, and listing publishers.
- `backend/search_db.py`: `publish_delta`, `sync_deltas`, `compact_database`.
- `backend/library_build.py`: resumable bootstrap.
- `backend/upload_hash_snapshot.py`: rebuild and deletion-epoch validation.
- `backend/index_work.py`, `backend/index_publication.py`: work and publication
  guards; `docs/index-maintenance-migration.md`: current guarantees and limits.

No production code was changed for this assessment. The following existing
focused suites passed: **157 tests in 26.06 seconds**.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_incremental_search_index.py \
  backend/tests/test_search_db_delta.py \
  backend/tests/test_people_embedding_index.py \
  backend/tests/test_tag_embedding_index.py \
  backend/tests/test_upload_hash_snapshot.py \
  backend/tests/test_inline_index_merge.py -q
```

These tests establish current behavior on local fixtures. They do not validate
the proposed extensions, complete mutation coverage or production performance.
