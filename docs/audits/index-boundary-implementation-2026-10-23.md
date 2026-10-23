# Indexer ownership implementation — 23 October 2026

The first delivery slice is implemented locally. No resources have been deployed.
The library-wide upload-wave protocol, deferred Vision release, one-shot
clustering and sequential app residency are still pending.

## Changes and compatibility

- `backend/index_ownership.py` defines the publisher boundary. Only
  `index-worker` may build/publish catalogs; Vision may publish its model-owned
  tag embeddings when its encoder is available. Invalid legacy publisher-role
  configuration fails at startup.
- Guarded entry points include vector/people/tag embeddings, Search deltas and
  bases, compaction, reconciliation, lexical/listing/sort/access/album/people
  builds, summaries, streaming sinks, bootstrap and upload-hash snapshots.
  Source writes and cluster's internal FAISS runtime remain outside this boundary.
- `enqueue_index_build` uses the existing durable ledger and dedicated queue.
  Missing configuration returns unavailable. Queue-send failure retains a
  recoverable intent; no fallback runs catalog work on cluster/library-ops.
- Legacy index/vector messages forward to the dedicated ledger. Handoff failure
  raises for queue retry. Acknowledging a forwarded request does not announce
  completion: job notifications omit aliases, and admin polling follows the
  authenticated library's canonical build job.
- Manual vector rebuilds return their existing result fields through the index
  worker. A failed builder returning `None` remains a failure, rather than
  claiming that an empty index was successfully published.
- A cold legacy cluster matcher can still reconstruct its internal candidates
  from source when the people-embedding blob is absent. It requests catalog
  maintenance separately and cannot publish that snapshot itself.
- Bicep requires dedicated indexing, sets the queue and publisher role explicitly,
  and regenerates the compiled deployment template. Deploy configuration,
  queue/scaler/worker and images together; an image-only legacy rollout is
  unsupported. Rollback requires the matching previous configuration and images.

No production matching algorithm, transport chunk size, wave membership,
assignment journal or projection-coverage protocol changed in this slice.
Existing early wakeups remain until the wave protocol replaces them.

## Local validation

From the repository root, the focused regression command passed **490 tests**,
with one explicitly deselected pre-existing failure:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_index_ownership.py \
  backend/tests/test_index_build_guard.py \
  backend/tests/test_index_maintenance_worker.py \
  backend/tests/test_index_update_scopes.py \
  backend/tests/test_worker_replica_floor.py \
  backend/tests/test_index_priming.py \
  backend/tests/test_index_notifications.py \
  backend/tests/test_admin_repair_async.py \
  backend/tests/test_album_persistence.py \
  backend/tests/test_upload_hash_snapshot.py \
  backend/tests/test_listing_index.py \
  backend/tests/test_explore_and_search_freeze.py \
  backend/tests/test_library_build.py \
  backend/tests/test_search_db.py \
  --deselect backend/tests/test_upload_hash_snapshot.py::test_light_build_queues_dedup_without_inline_scan
```

A further check of the final sink/publication guards passed **229 tests**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_index_ownership.py \
  backend/tests/test_search_db.py \
  backend/tests/test_listing_index.py
```

These selections overlap; their counts must not be added as unique coverage.
Tests explicitly disable publisher permission to check forbidden roles; the
existing suite's general builder fixture otherwise enables it for unit tests.
No global fixture was changed to make the new boundary checks pass.

The full backend run finished with **3,326 passed, 24 failed, 14 skipped and
28 setup errors**:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

All failing/error test identities were reproduced against unmodified commit
`64eceb3902fca000414bdde4ccb370bc87e06496`. The baseline selection produced
**24 failed, 35 passed and 28 setup errors**. The failures are:

- 19 lexical parity fixtures referencing the absent `search_utils.parse_exif_data`.
- Two rating/likes SQL ordering comparisons.
- One light-build test expecting an automatic dedup enqueue absent on baseline.
- Two asynchronous lexical rebuild tests that can acquire the idle lock before
  their background thread starts, then inspect unfinished output.
- 28 live-FAISS setup errors referencing the absent `app._metadata_scan_cache`.

Baseline reproduction: extract that commit's `backend` directory into a separate
scratch directory, then run from that directory using the workspace's interpreter:

```sh
PYTHONPATH=backend:backend/tests \
  /Users/rajat.verma1/photostore/.venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_lexical_search_parity_fixture.py \
  backend/tests/test_library_db_routes.py \
  backend/tests/test_live_faiss_app.py \
  backend/tests/test_upload_hash_snapshot.py::test_light_build_queues_dedup_without_inline_scan \
  backend/tests/test_lexical_index.py::test_touch_marks_dirty_and_serves_stale_while_rebuilding_in_background \
  backend/tests/test_lexical_index.py::test_get_user_lexical_index_cold_non_blocking_returns_none_then_builds_in_background
```

The full suite is **not green**; these baseline reproductions distinguish the
observed failures from regressions introduced by this slice. They do not prove
cloud rollout, scale-to-zero behavior or the future wave protocol.

The checked-in template compiles with the installed Bicep compiler:

```sh
DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/photostore-bicep-bundle \
  /Users/rajat.verma1/.azure/bin/bicep build deploy/main.bicep \
  --outfile deploy/azuredeploy.json
```

Compilation retains existing storage type/nullability warnings; no Azure
resource changes were made. `git diff --check` also passes.
