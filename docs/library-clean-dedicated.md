# Dedicated Clean library

Status: implemented 2026-11-15 in `backend/library_clean.py`, default engine (`LIBRARY_CLEAN_ENGINE=dedicated`;
`legacy` keeps the old per-photo path for one release).

Clean library is a direct admin action with strict resiliency and speed requirements, so it gets its own
module (`backend/library_clean.py`) with its own registry of what a library owns and its own bulk delete
primitives. It does not reuse the per-photo delete path (`_clean_one_photo`, `_delete_photo_blobs_if_present`,
`_cleanup_delete_row`) or the tracked `SourceTable` writes that index maintenance uses.

## Why: what today's clean costs and misses

Measured on 2026-11-14/15 with the same synthetic library mix as the main library (per photo: 0.79 embeddings,
0.92 face-by-filename rows, 0.71 faces and face embeddings, 0.29 people, plus metadata, image name, file hash,
one filename-owner row and one person-member row per face; 8.1 rows and 3 blobs per photo).

| 500 photos (4,063 rows, 1,500 blobs) | Today's clean, deployed cluster in East US | Dedicated prototype, from a laptop in Sweden |
|---|---|---|
| Wall time | 504 s (+65 s cold start) | 52.5 s |
| Storage calls | ~300,000 | 1,823 |
| Per call | ~25 ms | ~121 ms (transatlantic) |
| Left behind | 355 face embeddings, 1 index revision blob, 1 catalog lock blob | 1 catalog lock blob |

- 96% of today's calls (288,751) are `photojobs` point reads: the quiescence proof
  (`quiescence_check` → `_check_publication_claims`, 30 point reads) runs about five times per deleted row.
- It also rewrote `people-embedding-index` 1,000 times: each tracked face/person delete updates the live index
  that the end-of-clean purge then deletes anyway.
- Linear extrapolation to the 21,900-photo main library: about 6 hours in-region. Locally under the same latency
  model, a 500-photo clean failed after 11 minutes with `Library lease lost`.
- The prototype needs about 3.6 calls per photo; at 25 ms and 32 in flight that is a few minutes for 21,900
  photos (estimate, to be confirmed on Azure).

### Data today's clean leaves behind

Found by reading `_execute_library_clean` (origin/main 26700cc) against every table and container in a live
deployment. Items marked *measured* were reproduced on Azure; the rest are from the code.

| Left behind | Where | Impact |
|---|---|---|
| All face embeddings (*measured*) | `photofaceembeddings`, PK = library | Biometric data survives a clean |
| Every photo whose filename another library also holds (*measured via the prototype*) | `images`, `thumbnails` | `shared_names` skips the whole blob delete, but anonymous ids are per library (`get_anonymous_blob_name` keys on library + filename, and with blob tagging on the name carries a library tag checked by `blob_name_belongs_to_library`), so these blobs belong to nobody afterwards. Names like `IMG_0001.HEIC` collide often |
| Public album snapshots | `$web/s/<token>/` (page, manifest, thumbnails and previews) | Stay publicly reachable from the static website after the album rows are gone |
| Album share tokens | `photoalbumtokens`, PK = token | Dangling public tokens |
| Album members | `photoalbummembers`, PK = album key | Rows naming deleted photos |
| Library export zips | `library-exports/<library>/library-export-part-N.zip` | Full-resolution copies of every original survive |
| Preview cache | `thumbnails/preview/<physical>.jpg` | 2048px previews survive |
| Trash index | `phototrashindex`, PK = library | Rows naming deleted photos |
| Label dirty sets | `peopledirtyset`, `albumsdirtyset`, PK = `labels:<sha256(library)>` | Stale work items |
| Processing artifacts | `photojobs` PK `processing-artifacts-<digest(library)>`; `thumbnails/derived/v1/<sha256(library)>/…` and `processing-staging/v1/<sha256(library)>/…` | `collect_library_artifacts` deletes at most 50 manifests and 20 snapshot rows per call, so a large library keeps most of them |
| Wave history | `photojobs` PK `upload-wave-shadow-<digest(library)>` (waves, members, deletion proofs, inbox) | Grows forever |
| Index residue (*measured*) | `people-embedding-index/faiss-source-revisions/<sha256(library)>`, `lexical-index/catalog-publication-locks/<sha256(library)>` | Small, but outside every purge |
| Upload session registry | `photouploadsessions`, PK `library-<digest(library)>` | Stale sessions (see open questions) |
| Clustering activity marker | `photometadata` PK `clustering_maintenance`, RK = library | Stale marker |
| Workbench action log | `workbenchactions`, PK = library | Rows listing deleted filenames |

Two more costs hide in wave-owned libraries (every library created by a current deployment): before any
delete, `begin_library_cleanup` writes one deletion proof and one etag'd metadata merge **per photo,
sequentially** (3–4 calls each), and the synthetic benchmark did not exercise it (the synthetic library was not
wave-owned). At 25 ms per call that alone is roughly half an hour for the main library.

## Goals

- **Complete.** Every location a library can own is in one registry, and a test fails when a new table or
  container is added without a clean decision.
- **Fast.** Calls scale with rows / 100 (table transactions) and blobs / 256 (blob batches); the only
  per-photo call left is the filename-owner delete, because that table is partitioned by filename.
- **Resilient.** Every step is idempotent and re-derives its work from storage, so a crash, a lost message
  lease, throttling or a redeploy at any point is fixed by running the job again. Never reports success with
  residue.
- **Safe.** Keeps the fences that stop uploads, processing and index publication. Never deletes anything it
  cannot positively attribute to the library being cleaned. Never touches another library.
- **Observable.** Phase, counts and percentage on the job row while running; a final verification report.

Non-goals: Download library (separate design, same principles), single-photo delete (unchanged), and deleting
the library record itself. The module should take a `purge=True` flag later so library deletion can reuse it.

## The contract it keeps

These are the guarantees the rest of the system relies on; the dedicated clean keeps every one, only cheaper.

1. **Upload block.** The library row's `lastCleanupStatus = in_progress` and the queued/running job make
   `_library_cleanup_block_reason` refuse uploads. Unchanged.
2. **Wave fence.** `begin_library_cleanup` refuses while a wave is collecting, sets the wave policy's
   `cleanupState = 'cleaning'`, waits for running stage leases to drain, cancels unfinished waves, and marks
   every photo deleted. Vision and other source writers check the policy at every claim and final commit
   (`check_source_policy`, `source_policy_boundary`), so they are already refused. The per-photo deletion proofs
   and metadata merges stay, but **batched**: proofs go in entity-group transactions on the wave partition
   (99 proofs plus the control row per commit, the same optimistic concurrency `store._commit` uses today) and
   metadata merges in 100-row transactions with per-row etags. Same semantics, about 1/150 of the calls.
3. **Index fence.** `self_quiesced` raises the system fence, waits for in-flight writers to release their
   coverage reservations and for maintenance claims to end, and proves quiescence under the catalog lock.
   Unchanged. What changes is how often the proof is rechecked (below).
4. **Job lifecycle.** Queue message on `photostore-library-ops`, lease renewal, job-row heartbeat every
   `CLUSTERING_JOB_HEARTBEAT_SECONDS`, `LIBRARY_CLEAN_MAX_RETRIES`, completion email to members. Unchanged.

### Rechecking the proof: once per write round, and cheaply

Today every tracked row write reruns the full proof (catalog guard, quiescence row, 30 publication-claim point
reads). The dedicated clean keeps the rule that **no write starts after a failed or stale check**, but at round
granularity: it checks once, then issues one round of concurrent writes (up to 32 table transactions or blob
batches), then checks again before the next round. Every write in flight was admitted by a passing check; the
exposure is one round (~150 ms), comparable to today's gap between a per-row check and its write.

The check itself drops from ~32 calls to 3:

- the catalog guard's own lease state (in memory; its renewal thread does the I/O);
- one point read of the quiescence row (etag and `proofId` unchanged);
- two queries, `PartitionKey eq 'index-maintenance-pending' and libraryId eq '<library>'` and the same for
  `index-model-tag-pending`, instead of 30 point reads by hashed key (`purge_automatic_state` already relies
  on `libraryId` being on those rows).

## Registry

`library_clean.REGISTRY` lists every location with how the clean finds the library's data and in which phase
it goes. Kinds:

- `library_partition(table)`: PK = library id; wipe the partition.
- `derived_partition(table, key_fn)`: PK derived from the library id (digest/sha256/prefix); wipe it.
- `child_partitions(table, parent)`: one partition per row of a parent table (people → members, albums →
  album members, albums → tokens); enumerate parents **before** wiping them.
- `index_rows(table, filter)`: rows of a shared partition picked by column (`libraryId eq`, `RowKey eq`).
- `per_photo_blobs(container, names_fn)`: names computed from each metadata row.
- `blob_prefix(container, prefix_fn)`: list and batch-delete a prefix.
- `keep(reason)`: deliberately untouched.

| Location | Kind | Phase | Today |
|---|---|---|---|
| `photometadata` PK = library | library_partition | 4 (last) | yes |
| `photometadata` PK `clustering_maintenance`, RK = library | index_rows | 4 | no |
| `photoembeddings`, `photofaces`, `photofacebyfilename`, `photopeople`, `photoimagenames`, `photofilehashes`, `photoalbums`, `phototrashindex`, `workbenchactions` PK = library | library_partition | 2 | partly (no trash index, workbench) |
| `photofaceembeddings` PK = library | library_partition | 2 | **no** |
| `personmerges` PK = library, plus `merge-payloads/<payloadBlobName>` | library_partition + blob batch | 2 | yes, one by one |
| `photopersonmembers` PK = person id (from `photopeople`) | child_partitions | 2, before people | yes, one query per person |
| `photopersonmembers` `userId eq library` | index_rows | 4 (orphan sweep) | yes |
| `photoalbummembers` PK = album key (from `photoalbums`) | child_partitions | 2, before albums | no |
| `photoalbumtokens` PK = token (from album rows), plus `$web/s/<token>/` via `share_snapshot.delete` | child_partitions + blob prefix | 2, before albums | **no** |
| `photofilenameowners` PK = filename, RK = library | point delete per photo (phase 1); `RowKey eq library` sweep (phase 4) | 1, 4 | yes |
| `photojobs` PK = library (keep `library_clean` rows) | library_partition with keep filter | 2 | yes |
| `photojobs` PK `upload-wave-shadow-<digest(library)>` | keep: its per-photo deletion proofs are what a late writer settles against | — | kept |
| `photojobs` PK `processing-artifacts-<digest(library)>` | derived_partition | 3 | 50/20 per call |
| `photojobs` `index-maintenance-pending`, `index-model-tag-pending` rows with `libraryId` | index_rows (`purge_automatic_state`) | 5 | yes |
| `photojobs` `index-operator-quiescence` RK sha256(library) | the fence; lifted at the end | 5 | yes |
| `photosearchdirty` PK `automatic-<sha256(library\0kind)>` × 5 kinds | derived_partition (`purge_automatic_state`) | 5 | yes |
| `peopledirtyset`, `albumsdirtyset` PK `labels:<sha256(library)>` | derived_partition | 3 | no |
| `photouploadsessions` PK `library-<digest(library)>` | derived_partition (see open questions) | 3 | no |
| `images/<anonymousImageId>`; `images/<filename>` only when no other library owns the filename | per_photo_blobs | 1 | yes, but skips shared filenames entirely |
| `thumbnails/<physical>`, `metadata/<physical>.bin`, `grid/<physical>.webp`, `preview/<physical>.jpg` | per_photo_blobs | 1 | no preview |
| `thumbnails/derived/v1/<sha256(library)>/`, `thumbnails/processing-staging/v1/<sha256(library)>/` | blob_prefix | 3 | bounded GC |
| `covers/<sha256(library)[:16]>/` | blob_prefix | 3 | yes |
| `library-exports/<library>/` | blob_prefix | 3 | **no** |
| Index containers (vector, lexical, tag-embedding, people-embedding) | existing `delete_user_*_index_data` and `purge_immutable_artifacts` | 5 | yes |
| `people-embedding-index/faiss-source-revisions/<sha256(library)>` | blob_prefix | 5 | no |
| `lexical-index/catalog-publication-locks/<sha256(library)>.json`, `people-embedding-index/faiss-assignment-locks/<sha256(library)>.json` | deleted after the clean releases them (a lease someone else holds makes the delete fail; the next acquire recreates the blob) | 6 | no |
| `photolibraries`, `photomemberships`, `photoinvites`, `photousers`, `photostoreconfig`, `photoaudit`, `photolibraryclean` | keep: the library and its people stay | — | kept |

**Blob ownership.** A per-photo blob name comes from the photo's own metadata row in this library's partition.
Anonymous ids are per library, so anonymized blobs are deleted without a sharing check. (The library tag that
`blob_name_belongs_to_library` checks is not used as a gate: ids issued before tagging was switched on are
untagged and would be left behind.) Filename-named (pre-anonymization) blobs are deleted only when the
filename-owner table shows no other library for that name: one partition query per legacy photo, run
concurrently, and none for anonymized photos (all current uploads).

## Algorithm

Each phase streams pages, never materializes the library, and can be rerun from the start.

0. **Admit.** Job row `running`, library row `in_progress`. `self_quiesced`, then `begin_library_cleanup`
   (batched, above). Progress (phase, photos, rows and blobs deleted, storage calls) is merged into the job
   row's `progress` field every 10 s and at each phase end; it only updates an existing row, never creates one.
1. **Photos.** For each metadata page (1,000 rows): compute every per-photo blob name, batch-delete them (256 per
   request, a failed batch is split in half recursively down to single deletes; 404 is success), and delete
   each photo's filename-owner row (point delete, PK = filename). Metadata rows stay so a retry can find every
   blob again.
2. **Library tables.** Enumerate child partitions first (people → members, albums → album members and tokens,
   token index rows naming the library → tokens of albums an earlier clean already deleted, tokens → `$web`
   snapshots), then wipe every `library_partition` in the registry with 100-row transactions.
3. **Derived partitions and prefixes.** Processing artifacts, label dirty sets, upload session registry (except
   its `migration` cursor), derived/staging/covers/export prefixes.
4. **Metadata last, then orphan sweeps.** Wipe the metadata partition and the `clustering_maintenance` row.
   Then sweep `photofilenameowners` for `RowKey eq library` and `photopersonmembers` for `userId eq library`;
   both are full-table scans, run once, and catch rows whose photo or person row was already gone.
5. **Indexes and automatic state.** Existing index purges, `purge_immutable_artifacts`,
   `purge_automatic_state`, the FAISS revision blob, and `invalidate_image_names_cache` /
   `_invalidate_metadata_scan_cache`.
6. **Verify, then finish.** Count every registry location (excluding kept rows). Anything left triggers phases
   1–5 once more. If residue remains after that, the job fails with the residue list in its result and the
   fence lifted, and `finish_library_cleanup` is not called, so the wave fence stays up until a retry succeeds;
   it never reports done with data left. On zero residue: `finish_library_cleanup`, lift the fence, delete the
   lock blobs, library row `completed`, job `done` with counts and per-phase timings, completion email.

Order rationale: blobs before rows (rows are how blobs are found), child partitions before parents (parents
are how children are found), metadata last (it is how everything per-photo is found).

## Resiliency

- **Idempotent writes.** Deletes treat 404 as success. A failed transaction is split in half recursively down
  to single deletes, so one bad row never blocks 99 good ones.
- **Retries.** Exponential backoff with jitter on 408, 429, 500, 503 and connection errors (Table
  `ServerBusy`, `OperationTimedOut`; Blob batch sub-request failures). Each call has a timeout; a batch that
  times out is split and retried rather than repeated whole.
- **Throttling budget.** Azure Tables allows about 2,000 entities/s per partition and 20,000 transactions/s
  per account, and a 100-op transaction counts as 100 entities. Cap in-flight transactions at 3 per partition
  (about 2,000 entities/s at ~150 ms per 100-row transaction) and 32 overall, and wipe different tables'
  partitions in parallel to use the rest. The app's table and blob clients already pool 64 connections
  (`STORAGE_CONNECTION_POOL_MAXSIZE`), above the clean's 32 workers.
- **Crash anywhere.** The queue message reappears; the retry adopts the system fence (existing
  `self_quiesced` behavior) and reruns from phase 0. Every phase re-derives its work from storage. An
  abandoned system fence is lifted after `SYSTEM_FENCE_MAX_SECONDS` (6 h), and a clean still running behind
  it fails its next proof check instead of racing new writers.
- **No live index work during the wipe.** Writes go to the raw table clients, never through `SourceTable`, so
  nothing triggers live FAISS updates, the clustering library lease, or per-row coverage reservations. The
  index state is purged once in phase 5.
- **Bounded memory.** Pages of 1,000 rows; the only collections held are the person, album and token ids.
- **Failure isolation.** A store that keeps failing doesn't stop the others; it shows up as residue in phase 6.

## Performance model

Per photo: one filename-owner delete, 8.1 rows / 100 per transaction, about 6 blob names / 256 per batch, and 2
wave-fence writes / 100: about 1.15 calls. Per person: one member-partition query plus its transaction, about
2 calls. For the main library (21,900 photos, ~6,400 people) that is roughly 38,000 calls; at ~25 ms with 32 in
flight, about 30 s of storage time. The largest single partition (metadata, 219 transactions at 3 in flight)
takes about 11 s. So a few minutes end to end including the fence wait, against about 6 hours today. These are
estimates from measured per-call latency, to be confirmed by the in-region run.

The filename-owner delete is now more than half of all calls. Re-keying that table by library would remove it
but touches the upload path; out of scope here.

## Measured

The implementation on Azurite with the same latency model as the legacy runs (median 12 ms, 1% spikes):

| Photos (rows) | Legacy | Dedicated |
|---|---|---|
| 100 (810) | 165 s, 61,359 calls, face embeddings left | 8.1 s, 395 calls, nothing left |
| 2,000 (16,258) | — (500 photos failed after 11 min) | 49 s, 3,824 calls, nothing left |

At 2,000 photos, 36 of the 49 s are phase 1, and nearly all of that is Azurite's own blob batch handling
(~10 s per 256-blob request; on Azure the same batch took ~1 s from Sweden). The synthetic libraries are not
wave-owned, so the batched `begin_library_cleanup` is covered by a unit test (250 photos: 3 proof commits and 3
metadata transactions instead of ~750 sequential calls), not by these timings. An in-region Azure run is still
to do.

## Code shape

- `backend/library_clean.py`: `REGISTRY` (entry kinds `Photos`, `Children`, `Tokens`, `Payloads`, `Partition`,
  `Point`, `Rows`, `Prefix`, `Keep`), `Run` (write pool, per-partition slots, `gate()` proof check per round,
  `delete_rows` with split fallback, `delete_blobs`, `delete_prefix`, `verify`) and `run(library_id, stores)`.
  `Stores` carries raw table/blob clients and callbacks so the module imports nothing from `app`.
- `backend/processing_cleanup.py`: `begin_library_cleanup` writes proofs and metadata merges in transactions
  (shared by library deletion).
- `backend/automatic_index_updates.py`: `_check_publication_claims` uses the two `libraryId` queries.
- `backend/app.py`: `_execute_library_clean` keeps its catalog lock and quiescence decorators and dispatches on
  `LIBRARY_CLEAN_ENGINE`; `_dedicated_library_clean` wires `Stores`; `_run_library_clean` sets the job id for
  progress and deletes the lock blobs after the fence is lifted. `_legacy_library_clean` is the old body.

## Tests

`backend/tests/test_library_clean.py`:

- **Registry completeness:** every `*_TABLE` and `BLOB_*_CONTAINER` constant in `app.py` (and `$web`) must have
  a `REGISTRY` entry or an explicit `Keep`, and the app must hand the clean every store it wipes.
- **End to end:** one of everything a library can own, beside a second library holding the **same filenames**;
  the clean leaves exactly the kept rows and the other library untouched. Also: legacy filename blobs shared
  with another library survive, a token row naming another library survives, and a rerun on a library an
  earlier clean emptied removes the face embeddings and public snapshots it left.
- **Bounds and faults:** transactions of at most 100 rows in one partition and blob batches of at most 256; a
  refused transaction falls back to single deletes; a 503 is retried; a failed proof check stops the writes
  within one round and leaves metadata for the retry; data that survives a second pass, or a blob the service
  keeps refusing, fails the job with the residue.
- **Wiring:** the engine dispatch, begin → run → finish ordering (no finish after a failure), progress updating
  only an existing job row, batched `begin_library_cleanup`, and the two-query claims check.

The legacy tests (`test_library_shadow_cleanup.py`, `test_library_clean_shared_names.py`) pin
`LIBRARY_CLEAN_ENGINE=legacy` while that engine exists.

## Rollout

1. Ships with `LIBRARY_CLEAN_ENGINE=dedicated` as the default; `legacy` stays for one release.
2. Running Clean library again removes most residue earlier cleans left: face embeddings, export zips, preview
   cache, the hash-keyed partitions, and public snapshots (found through token rows that still name the
   library). Anonymized blobs a legacy clean skipped for shared filenames cannot be found again once their
   metadata is gone; that needs a separate blob sweep.
3. Delete `_legacy_library_clean` and `_cleanup_library_shadow_rows` after the legacy release.

## Decisions

1. **Upload session registry:** wiped, except its `migration` cursor (dropping it would rerun that migration).
2. **Wave history:** kept. Its per-photo deletion proofs are what a late processing message settles against
   after the policy fence is cleared; wiping them is not safe without a change on the wave side.
3. **Public albums:** revoked with their albums: token rows that name the library, and their `$web` snapshots.
4. **Export zips:** deleted (they are full copies of the originals).
