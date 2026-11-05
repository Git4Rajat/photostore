# Exact incremental summaries: search SQLite and raw-stream utility

## Status and scope

[summary_incremental.py](../backend/summary_incremental.py) now contains two
explicitly separate adapters. `SummaryContributionStore` remains the standalone
raw-stream utility described below. `DatabaseSummaryAdapter` is integrated
inside the search SQLite base and its existing delta transactions, with automatic
worker publication/replay after every successful drain, including noop. Verified
against current code on **2026-11-01**: this is not an unwired handoff or an
opt-in-only summary path. The existing raw full builders remain valid and are
not replaced with reduced-row semantics; full builders remain available for
bootstrap, bulk work, repair and incompatible bases.

### Operational compatibility

**With durable updates active, existing library/account cleanup and even normal
library-content wipes require explicit operator proof that ALL library writers
are stopped. Existing emailed approvals alone do not meet this requirement.**
The authenticated admin route `POST /api/admin/indexes/source-reservations`
provides `fence`, `prove`, `recover` and `resume` actions. Proof requires
`fenceId`, a nonempty `reason`, `confirm=ALL_LIBRARY_WRITERS_STOPPED`,
`allWritersStopped=true`, `noPausedRequests=true` and
`legacyWritersStopped=true`, after externally stopping/draining all writers and
paused requests. Keep them stopped through recovery/purge; failures retain the
fence. Orphan source reservations and the fence/proof **never time out**.
See [incremental-index-upgrades.md](incremental-index-upgrades.md) for the full
operator lifecycle, immutable GC policy and coverage contracts.

`DURABLE_INDEX_UPDATES` is a code boolean derived at import time from a nonempty,
stripped `INDEX_MAINTENANCE_QUEUE_NAME`, not a separate environment-variable
opt-in. Automatic work reuses existing jobs/queue and `searchdirty` storage.
All running source writers must use the tracked-client version; mixed-version
or external raw writers invalidate the coverage assumptions. This documentation
update changes no code/infrastructure and performs no deployment; it adds no
artifact overlays or expansion of Core ownership.

## Search-database integration

* `DatabaseBuilder.add` seeds normalized Explore memberships from exactly the
  `iter_smart_rows` reduced projection in its existing stream. It restores
  SQLite REAL coordinates (including NaN-to-NULL), combines reduced subject
  tags/tags, and removes raw objects. Row ids preserve the SQL iteration order,
  including repeated filenames and subsequent delete/reinsert order.
* `summary_members` retains per-row membership occurrences and indexed
  `(kind,key,id,occurrence)` candidate order. `summary_groups` materializes
  counts and first label/filename/coordinates with indexed top-24 ordering.
  Old contributions are removed and new contributions added **in the same
  transaction** as rows, FTS, people and `delta_seq`. Only touched keys are
  repaired, using indexed `LIMIT 1` reads.
* `summary_days` stores UTC capture-day counts, with zero for undated rows.
  Timeline reads those day buckets, not full `COUNT/GROUP BY rows`. Covers keep
  the exact existing SQL period queries, positive-likes boolean, SHA-derived
  63-bit rank and original `capture_year/capture_md` semantics. They are **not**
  reduced from streaming/day cover winners. Today rollover requires no row
  rewriting.
* `SearchDatabase.summary_snapshot` pins Explore winners, day counts, covers
  and committed sequence to one SQLite read transaction. The source token
  includes a persisted base identity, the actual DB sequence and today. Backup
  preserves that identity; full rebuilding changes it. Compaction records the
  sequence **inside the backup**, including a delta committed during backup.
* [summary_runtime.py](../backend/summary_runtime.py) hydrates the selected
  groups via the unchanged `ExploreAccumulator.finalize` helpers (same people
  names, point reads, missing-metadata drops and no backfill). Indexed Explore
  and timeline artifacts share a `database-reduced-summary-v1` mode and source
  token. The raw full streaming sinks explicitly mark `raw-stream-summary-v1`
  and retain their original algorithms, including their different timeline.
* `_refresh_library_summaries` is the worker's post-successful-delta-drain hook,
  **wired for every successful `delta` or `noop` drain**, including drains
  without compaction and `more` passes that subsequently converge. It verifies
  both existing best-effort stores with bounded artifact reads and raises on
  failed publication. The maintenance worker holds the universal real renewable
  library catalog lock shared by all publishers, including full/wave, hash and
  model-tag paths. Summary failure retains retryable WorkStore work and pending
  People/Albums dependency dirty IDs. A delta may already be committed when
  publication fails: **noop replay republishes from the committed SQLite
  snapshot** before maintenance can succeed. The adapter itself does not discover
  dirty IDs or acknowledge maintenance receipts. The two derived blobs are not
  an atomic remote pair; their source tokens identify the common SQLite snapshot.
* Old DBs without `summary_schema` raise `SummarySchemaMissing`; no request or
  incremental delta path silently scans them. **Explicit compaction** seeds
  their aggregates in the backed-up file and publishes the upgraded base. The
  worker schedules that compaction on a missing-schema refresh while leaving
  the original work retryable; it does not acknowledge a failed summary refresh.
  Full-builder test adapters exposing only `iter_smart_rows` remain supported
  only by `_finalize_library_summaries`, not the incremental refresh hook.

Integration tests in
[test_summary_database_integration.py](../backend/tests/test_summary_database_integration.py)
compare random updates to the previous full reduced Explore and production SQL
timeline algorithms, including UTC/local boundary dates, SHA cover rank ties,
likes, NaN coordinates, missing covers, rollover, deletion/replacement order,
rollback, concurrent snapshot/replay transactions, legacy compaction, backup
sequence races and SQL traces rejecting hot full scans.

Runtime tests in
[test_automatic_runtime_integration.py](../backend/tests/test_automatic_runtime_integration.py)
exercise the real SQLite hook on noop, delta and multi-pass drains without
compaction/full scans, summary publication failure/replay, retained dependency
dirty IDs and missing-schema compaction scheduling. Immutable-artifact GC runs
after successful maintenance completion/claim release and during the existing
60-second index-worker recovery scan; it is not a summary read-side hook.
Eligibility is **per artifact family**, requiring only that family's exact
idle/full-certified control, matching manifest lineage and complete bounded
retention survey, not all five controls. Missing/stale unrelated controls do not
block eligible families. The same real renewable catalog lock, global active
maintenance/model-tag claim gate and conservative related-builder ETag checks
remain required. Reports expose per-kind outcomes and aggregate
`complete`/`partial`/`deferred`/`capped` status; default grace is 24 hours with a
global hard cap of 100 blob deletions per call. Periodic discovery accepts any
canonical supported family control, not only certified hash controls, and uses
bounded rotating deduplication (default 25 libraries per recovery). See the
[GC policy](incremental-index-upgrades.md#universal-publication-lock-and-immutable-gc)
for retention, sidecars, configuration and fail-closed checks.

Publication and source writes are not one atomic blob/table transaction. Readers
may temporarily observe a stale, uncertified artifact pointer if a source writer
overlaps publication. The source-revision CAS then prevents certification and
dirty acknowledgement, and the next automatic artifact attempt performs a full
repair. Summary read tokens likewise describe their pinned SQLite snapshot,
not a linearizable snapshot of live metadata hydration or an atomic remote pair.

## Standalone raw-stream utility

It incrementally maintains exactly the aggregates of
[ExploreAccumulator](../backend/app.py) and
[TimelineAccumulator](../backend/timeline_metadata.py) for an explicitly
admitted, explicitly ordered stream. Differential tests compare results to
those existing builders, not to a newly invented reference algorithm.

## Research: the full paths are not interchangeable

* Explore takes city + country, then address, then the first eight characters
  of both coordinates. Phrase normalization folds NFKD to ASCII, lowercases,
  replaces punctuation with spaces and collapses whitespace. Display titles
  replace underscores and title-case only all-lowercase strings.
* Tags/objects pass through the existing `parse_tags` / `parse_json_list`
  helpers: canonicalization, singularization, single-token restriction,
  configured length/count caps and deduplication all remain in force. There
  is **no additional Explore confidence or minimum-count threshold**. Raw
  contributions must not substitute `effective_tags` or a reduced search row.
* Explore deduplicates parsed terms before its final phrase normalization,
  then increments per surviving term, not necessarily per unique normalized
  photo/group pair. Membership occurrences are retained separately.
* Covers, labels and coordinates come from the **first stream occurrence**.
  Top 24 per kind is count-descending with stable first-occurrence ties. A
  deleted cover must promote the next candidate's label and coordinates too.
* Finalization point-reads metadata for the selected covers. Missing metadata
  drops a selected group; it does **not** refill from rank 25. The utility's
  hydration callbacks retain this behavior and deduplicate cover reads.
* The streaming timeline counts every supplied row, even an absent filename.
  It uses `metadata_capture_datetime` (including EXIF/QuickTime and client
  modified/upload fallbacks), then **capture-local `.date()`**. Undated and
  future rows count separately. Covers require a stripped nonempty filename;
  `int(float(likes)) > 0` wins, then 32-bit FNV rank, then filename. Likes
  magnitude is not a ranking factor. The shared cover helper is reused.
  Undated/nameless rows never evaluate likes. An infinite likes value on a
  future row retains a deferred error candidate (`liked_order = -1`): rendering
  its day after rollover raises the same `OverflowError` as the full builder,
  rather than silently producing an approximate cover.
* [SearchDatabase.timeline_summary](../backend/search_db.py) is a
  **different reference**: admitted search rows only, stored UTC capture day,
  SHA-256-derived 63-bit cover rank and period-index queries with different
  tie behavior. Its cover year is stored separately from its UTC day.
  [iter_smart_rows](../backend/search_db.py) also reconstructs coordinates,
  merges reduced subject tags and tags, and discards objects. Thus neither raw
  Explore nor the streaming timeline is generally equivalent to the full
  local-database summary path. A regression test demonstrates this date
  difference explicitly. The standalone `SummaryContributionStore` does **not
  claim SQLite timeline equivalence**; the separate database adapter above
  preserves that SQL reference instead.

## Explicit API contract

1. Allocate a fresh local SQLite file for one library and one reference
   stream. Use `SummaryContributionStore`; close it or use its context manager.
   Keep the file on local disk, not SMB/Azure Files.
2. Feed `contribution_from_row(row, identity=..., position=...)` into `seed`,
   supplying an explicit `source_version`. This is the only bootstrap path.
   The caller certifies completeness and uses exactly the full builder's
   filtering/projection. The utility does not scan metadata/search SQL or
   infer admission. Failed or duplicate streams roll back; a seeded sidecar
   cannot silently be reset. A reseed requires a new file.
  Parser/helper versions and configured tag caps must remain the same across
  seed and updates; a semantic/configuration change requires a fresh seed.
  The format token does not automatically fingerprint runtime configuration.
3. `identity` identifies one stream occurrence, not necessarily a filename.
   `position` is a unique nonnegative integer representing the **reference
   full-stream order**, not arrival order. Duplicate filenames or nameless
   timeline rows need separate identities. Preserve positions on ordinary
   updates. Insertions require a certified position in the intended stream;
   new arrivals are not automatically appended. If future full builds reorder
   existing rows, this sidecar cannot promise equivalence without explicit
   position updates or a new full-stream seed.
4. Construct an iterable of explicit `(before, after)` contributions and call
   `apply(previous_source_version=..., source_version=...)`. `None` denotes
   insertion/deletion. `get(identity)` is an indexed local lookup for the
   before contribution. Missing before state is **not** silently treated as
   an insertion. Identity changes require delete + insert. Source versions
   must be nonempty and different; they are opaque caller-certified tokens,
   not automatically validated against a remote manifest.
5. The whole batch and source token commit in one `BEGIN IMMEDIATE`
   transaction. Exact serialized before-state comparison and a source-token
   check reject stale/conflicting batches. Replays fail closed, not
   idempotently successful. Partial generators, duplicate positions, and
   invalid changes roll back. Across writer instances, SQLite serializes
   commits; there is no remote lease or cross-file transaction.
6. `explore_groups(limit=24)` returns exact pre-hydration candidates with
   filename/count/label/coordinates. `explore_summary(fetch_metadata=...,
   build_photo=...)` returns places/things in the full builder's shape. The
   caller must supply the same user-scoped metadata reader and photo
   projection/people-name context as app finalization. No Azure reads are
   embedded in this module. `timeline_summary(now=...)` returns the streaming
   timeline shape; injectable `now` follows the accumulator's `.date()` rule.
   Future dates become eligible on rollover without per-photo rewriting.

## Disk state and bounds

* `photos`: primary-key lookup of the full **compact contribution**, unique
  stream-position index. Heavy metadata/embeddings are not retained.
* `members`: identity/occurrence primary key plus indexed
  `(kind, key, position, occurrence)` candidate order. A cover deletion or
  edit is repaired with an indexed `LIMIT 1`, never a group membership scan.
* `groups`: materialized occurrence counts and first candidate payload;
  `(kind, count DESC, first_position, first_occurrence)` supports indexed top
  groups. Zero-count groups disappear. Labels/coordinates are repaired too.
* `days`: materialized counts, with ordinal zero representing undated rows.
* `covers`: per-photo dated timeline candidates, indexed by
  `(day, liked_order, rank, filename)`. Summary rendering reads distinct day
  counts and one indexed winning candidate per eligible day, reducing the
  winners to exact month/year winners. It never scans photo rows. Day counts
  include rows without cover candidates.

Update cost is proportional to supplied contribution occurrences and affected
keys, with indexed probes (and SQLite index-maintenance cost), independent of
the number of photos in a group. Bootstrap remains an explicit O(full stream)
pass and disk state is O(photo contributions + memberships). Python seed/update
memory is O(one contribution + its affected keys), provided the caller supplies
a generator rather than retaining a library/batch. SQLite page cache is capped
at roughly 4 MiB per connection; transaction journals still consume disk.
Summary memory/time scales with distinct day output plus at most 48 Explore
winners; generating all timeline days cannot be constant-size. Separate summary
calls use separate read snapshots. Hydration reads live metadata outside the
sidecar transaction, like the existing full finalize path, and is not a remote
snapshot guarantee.

## Standalone sidecar limitations

No automatic changed-photo discovery, source fencing, refresh scheduling,
manifest CAS, blob persistence/download, replica cache management, retention,
or benchmark against a production library is included **in the standalone
raw-stream utility**; this is not a claim that the integrated runtime lacks
automatic coverage, scheduling, publication or GC. Changes which only
affect hydrated photo properties still require rehydration even when their
aggregate contribution is unchanged. A local sidecar can be lost and must be
explicitly reseeded. It must not be attached to existing index delta receipts
unless their completeness, row projection and ordering contracts are certified.

The standalone sidecar must not be substituted for the local-search-DB path.
That path uses the integrated reduced-row adapter and UTC/SHA/period SQL
semantics described above, not raw contributions. Full builders are not
retired or bypassed.

Tests in [test_summary_incremental.py](../backend/tests/test_summary_incremental.py)
cover exact full-builder differential output, cover deletion and replacement,
group disappearance, count ties, repeated contribution counts, parser rules,
likes thresholds, nameless/undated/future dates, date rollover, random updates,
missing-metadata behavior, transactional failures, disk reopening, source
conflicts, SQL query plans and streaming Python-memory bounds.

## Remaining limitations and validation

Deployment-scale RSS, CPU, local disk, source reads, artifact bytes and latency
still require measurement on deployment-size machines. Indexed summary output
scales with distinct days and selected groups; compatible whole-artifact index
rewrites still scale with artifact size. Tests are not million-photo sizing or
live distributed concurrency/load certification. Every running source writer
must use the current tracked-client version, and ordinary cleanup still needs
the explicit external all-writer shutdown/proof described above.

Latest agent-reported full backend suite after feedback 1, 2, 3 and 6:
**5,300 passed, 14 skipped, 12 existing warnings**; all skips require the
unavailable private TIFF corpus. Frontend is unchanged; its last verified run
was **816 passed across 57 files**, a prior run, not a rerun of the current state.
Neither suite was rerun for this documentation-only update. No production/code
edits, infrastructure change or deployment was performed.