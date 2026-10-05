# Backend API architecture (as of 2026-08-29)

Scope: the synchronous HTTP request path served by the `backend` Container App
(gunicorn, `APP_ROLE=backend`) — process/thread model, data layer, per-request
auth cost, caching, and where API latency actually goes today. Everything
below is verified against current code (`backend/app.py`, `backend/storage_utils.py`,
`backend/entrypoint.sh`, `deploy/resources.bicep`), not recalled from memory.
Background photo processing (`worker`, `ipworker` roles, same codebase, different
entrypoint) is out of scope — see `docs/ipworker-architecture.md` for that half.

This doc exists for the same reason `docs/ipworker-architecture.md` does: so the
next round of "why is the API slow" investigation starts from verified facts
instead of re-deriving them. Keep it current when the facts below change.

## 1. What `backend` is

A single Flask app (`backend/app.py`, ~14k lines) that also contains the
`worker` and `ipworker` roles — `entrypoint.sh` picks which one runs via
`APP_ROLE`. In the `backend` role it's served by gunicorn and does nothing but
answer HTTP requests: no queue polling, no clustering, no image inference in
its main loop. **226 routes** (`grep -c '^@app.route'`), covering auth,
library/multi-tenant management, uploads, photo listing/search, people/faces,
albums, admin/backfill tools, and public share links.

There is no SQL database and no Cosmos DB. All structured data lives in
**Azure Table Storage** (`azure.data.tables`); all media bytes live in
**Azure Blob Storage** (`azure.storage.blob`). This shapes almost everything
below — no joins, no server-side filtering beyond `PartitionKey`/`RowKey`
equality and simple OData predicates, and "list X for this user" almost always
means "download every row in this partition, filter/sort/paginate in Python."

## 2. Process / thread model (`backend/entrypoint.sh`, `deploy/resources.bicep:453-538`)

- **1.25 vCPU / 2.5Gi memory per replica**, `minReplicas: 0`, `maxReplicas: 5`,
  scaled by an HTTP concurrency rule (`concurrentRequests: 8`).
- **`GUNICORN_WORKERS=1`, `GUNICORN_THREADS=4`**, `gthread` worker class.
  Deliberately 1 process, not N: gunicorn forks *before* importing the app (no
  `--preload`), so every additional worker process would separately load its
  own numpy/scipy/scikit-learn/Pillow/Azure-SDK baseline (~250-350MB) and
  prime its own vector-index cache. Threads share one Python process's memory
  and module state; processes don't. Concurrency is bought with threads
  instead of processes for this reason.
- **This means the whole replica shares a single GIL.** 4 threads give real
  concurrency for I/O-wait (a table query, a blob read, waiting on Azure) but
  serialize on any CPU-bound Python stretch (JSON parsing, numpy work,
  synchronous image decode). This is not a hypothetical — see §6.
- **Worker recycling**: `--max-requests 400` (±50 jitter), `--graceful-timeout 60`.
  Long-lived workers fragment their heap on image/RAW/video processing and
  creep upward in RSS until OOM; periodic recycling bounds that. With
  `--workers 1` a recycle is a capacity-zero blackout until the sole worker
  drains or is force-killed — bounded to ≤60s by the graceful timeout (was up
  to ~120s before this was tuned; see §7).
- **`--timeout 600`, `--keep-alive 30`**: the app is I/O-bound enough (table/
  blob round-trips, some endpoints doing many of them per call) that a long
  per-request timeout is treated as normal, not a bug to route around.

## 3. Data layer

### Tables (`_init_storage_clients`, `backend/app.py:898-1063`)

One `TableServiceClient`, one client per logical table: `photometadata`,
`albums`, `faces`, `people`, `merges`, `imagenames`, `hashindex`,
`filenameowners`, plus multi-tenant tables (`users`, `libraries`,
`memberships`, `invites`, `audit`, `cleanrequests`) and a queue service client
shared with `worker`/`ipworker`. Almost every per-user table uses
`PartitionKey = user_id` (the *library* id, not necessarily the login
identity — see §4), `RowKey = filename` or `RowKey = personId/faceId`. This
partitioning means:
- A single-entity lookup (`get_entity(partition_key, row_key)`) is a cheap
  point read — used throughout (`_get_metadata_entity`, membership checks,
  etc.).
- "List everything for this user" (`query_entities("PartitionKey eq '{user_id}'")`)
  is a **full partition scan** — the only way to list photos, faces, or
  people, since there's no secondary index or server-side pagination in Azure
  Table Storage's query model here. Every listing endpoint pays for this
  proportionally to library size, not to page size requested.

`face_table_client` / `person_table_client` are wrapped in
`_InvalidatingTableClient` (`backend/app.py:811`) — any write through them
auto-invalidates the relevant per-user cache entry (§5) by inferring the
`PartitionKey` from the call arguments, so callers don't have to remember to
invalidate manually.

### Blob storage and media URLs

Two modes, both present in the code (`backend/app.py:7589-7767`):
- **Proxy mode**: `/api/photos/{thumbnail,preview,image,cover}/<filename>`
  streams bytes through the backend container. Comment in code: "Streaming
  media bytes through this container dominated its compute bill" — this is
  why SAS mode exists.
- **SAS mode** (`MEDIA_URL_MODE='sas'`): the browser gets a direct
  read-SAS URL to blob storage, bypassing the backend for the actual bytes.
  Two things keep this cheap rather than one SAS-mint round-trip per URL:
  - The **user-delegation key** is minted once per UTC day and cached
    in-process plus persisted as a row in the metadata table
    (`_stable_delegation_key`, `backend/app.py:7638`), so every
    replica/worker signs with the *same* key instead of each minting its own.
  - SAS start/expiry are **day-aligned** (`[day start - 15min, day start + 48h]`),
    so a given blob's URL is byte-identical across requests all day —
    the browser's HTTP cache stays effective instead of busting on every
    page load.

## 4. Request lifecycle for a typical authenticated call

Every non-public route goes through the same tenant-isolation boundary,
`_require_library_context` (`backend/app.py:1683`), usually via the
`_require_user_id` compatibility wrapper:

1. **Session token validation** (`_resolve_session_payload`, `app.py:1645`) —
   in-process HMAC/JWT-style verification (`password_auth.validate_session_token`),
   no storage call.
2. **Account lookup** — `library_store.get_user_checked(user_id)`, a Table
   Storage point read, wrapped in `_auth_lookup_with_retry` (3 attempts,
   50ms/100ms/200ms backoff, `app.py:1664`) so a transient storage blip
   surfaces as a retryable 503 instead of a spurious 401.
3. **Token-version check** (in-memory, from the account row already fetched).
4. **Membership lookup** — *only* if the active library differs from the
   caller's own id (i.e. shared-library access, not the common case) —
   another point read, same retry wrapper.
5. Route handler runs.

So the common case is **one Table Storage point read per request** just for
auth, before the handler does anything — cheap individually (point read on a
partition key), but it's fixed per-request overhead paid by all 226 routes,
including trivial ones like `/health`... actually `/health` (`app.py:6216`)
does not go through this path, but most `/api/*` routes do.

## 5. In-process caching layer

`_UserScanCache` (`backend/app.py:705`) is the core primitive: a per-user TTL
cache with request coalescing — the first caller for a given user does the
real partition scan while concurrent callers for the *same* user block on a
lock and reuse the result, rather than each re-scanning. Three instances share
this pattern and the same invalidation hook (`_invalidate_people_scan_cache`,
`app.py:774`, auto-wired to every face/person table write via
`_InvalidatingTableClient`):

| Cache | Backs | TTL (default) |
|---|---|---|
| `_person_scan_cache` | `_cached_person_rows_for_user` | `PEOPLE_SCAN_CACHE_TTL_SECONDS` = 20s (backend), 120s (ipworker override, `resources.bicep`) |
| `_face_summary_scan_cache` | face-partition lookups | same |
| `_people_embedding_index_cache` | `_load_people_embedding_index` | same (added 2026-08-29, commit `dd2a1b1`) |
| (separate) `_cached_metadata_rows_for_user` | photo-metadata partition scan | its own TTL, same coalescing shape |

**Important caveat for capacity planning**: these caches are plain
module-level Python dicts — **process-local, not shared across replicas**,
and (with `GUNICORN_WORKERS=1`) shared across all 4 threads of one replica but
not across the up-to-5 scaled replicas. A user's requests landing on
different replicas each pay a fresh scan on first hit. This is fine today
(`GUNICORN_WORKERS=1` means no intra-replica fragmentation) but would silently
regress cache hit rate if workers were ever scaled up per-replica without
also moving this to a shared cache (Redis, or the existing table-backed
pattern used for the SAS delegation key).

## 6. CPU-bound work under a shared GIL — a real, already-hit failure mode

`deploy/resources.bicep:470-492` documents a live experiment worth
internalizing before touching concurrency again: `GUNICORN_THREADS` was
raised `4→12` on `photostore-test` to fix slow `/upload/finalize`,
`/upload/init-batch`, and `/upload/client-processing` calls (each 20-90s+,
under a ~20-slot ceiling that looked saturated). Result: one genuinely
I/O-bound endpoint (`/upload/processing/claim`) got much faster, but the three
target endpoints got **worse** (p50 37-45s→60-67s, p90 85-94s→197-240s,
clustered at 240s — a gateway timeout signature, not organic slowness).
Conclusion: those handlers do enough CPU-bound Python work per call that more
threads increased GIL contention instead of relieving an I/O queue — reverted
same session.

The actual root cause was found and fixed 2026-08-29 (commit `dd2a1b1`,
**not yet deployed**): `_load_people_embedding_index` was rebuilding the
entire people-embedding index (JSON-decode + normalize every person's rep
embedding) from scratch, synchronously, on **every single uploaded photo** —
an O(library-size) CPU-bound operation inline in the upload request path,
invisible in CPU-percent metrics because it was fast per-call but ran
constantly during a burst. Fixed two ways, both measured:
- Cached the built index (new `_people_embedding_index_cache` above): 131ms →
  0.04ms per call on a 500-person library, cache hit.
- Vectorized `_best_two_person_matches` (`app.py:3005`): one batched numpy
  matmul across same-dimension embeddings instead of one Python similarity
  call per person. Full assign call: ~131ms → ~1.6ms.

**Takeaway for future optimization work on this app**: `htop`-style CPU% per
replica under-reports this class of bug, because the cost is O(library size)
Python work repeated per-request, not a single expensive call. When a handler
is slow under concurrency but CPU isn't pegged, check for per-request
full-partition rebuilds before assuming it's pure I/O wait and reaching for
more threads — more threads made it worse here.

## 7. Prior incidents & fixes (grounded in commit history / existing docs)

- **Gunicorn worker-recycle blackout**: `--workers 1` + periodic recycle had
  no second worker to cover the gap, producing up to ~120s capacity-zero
  windows (bursts of 503s). Fixed by bounding `--graceful-timeout` to 60s.
  Also fixed missing CORS preflight caching and a `fileParallelism`-stuck-at-1
  bug in the same investigation. Shipped `a6c3398`; `maxReplicas` raised 3→5
  in `e15adda`.
- **`/api/persons` and `/api/faces` were unpaginated full-account scans** —
  fixed with offset/limit/`namesOnly`/`q` params (Phase A/B split), verified
  live.
- **Upload dispatch-gate retry convoy**: a concurrency-2 cap + retry-in-place
  on `/upload/init-batch`/`/upload/finalize-batch` let one stuck request hold
  a slot for up to ~33 minutes, stalling the whole batch at 0.00 MB/s. Fixed,
  shipped `05f43ee`. Related commits (`d8583a5`, `4513977`, `22d7a5f`,
  `665c323`) show several iterations tuning that concurrency cap — currently
  bounded and adaptive to backend congestion rather than fixed.
- **Backend call-reduction survey**: fixed synchronous duplicate geocode work
  and batched lease-claims for the drain-loop's first wave; confirmed polling
  loops and per-item access URLs were already reasonable and left alone
  rather than "optimizing" code that wasn't the bottleneck.
- **Redundant read-modify-write removed from the upload finalize path**
  (uncommitted, `backend/storage_utils.py`, working tree as of this doc):
  `finalize_uploaded_file` used to `upsert_entity` the metadata row, then
  immediately call `_init_processing_status_for_image`, which re-read the
  same row via `get_entity` purely to set processing-status fields and
  `upsert_entity` it right back. Folded the status fields directly into the
  first `metadata` dict so the second read+write disappears — one fewer round
  trip per uploaded file, same final state. Not yet committed.
- **People-embedding-index CPU bottleneck** — see §6, `dd2a1b1`, committed but
  **not yet deployed** as of this doc.

## 8. Hot-endpoint patterns worth knowing before optimizing further

Two patterns found while writing this doc that fit the same "N per-request
storage round-trips" or "O(library size) work regardless of page size" shape
as the incidents above, neither yet flagged/fixed:

- **`list_photos` (`GET /photos`, `app.py:10959`) always loads and sorts the
  entire metadata partition**, even though it returns a `limit`-sized page
  (`entries[offset:offset+limit]`). The full-partition load is cached
  (`_cached_metadata_rows_for_user`) and the sort/filter is pure in-memory
  Python, so this is bounded by cache TTL rather than hitting storage every
  call — but CPU cost of sorting/filtering still scales with total library
  size on every cache-miss request, not with the page requested. Same
  backfill-on-read pattern also lives here (bounded per request via
  `UPLOAD_DATE_BACKFILL_MAX_PER_REQUEST` / `PHOTO_PROPS_BACKFILL_MAX_PER_REQUEST`,
  so it's self-limiting, not unbounded).
- **`/api/photos/access-batch` (`app.py:7536`) does one sequential
  `_get_metadata_entity` point-read per filename**, in a plain Python `for`
  loop, for up to 2000 filenames per call (`app.py:7548`'s own limit). Each
  point read is cheap individually, but they're not batched or parallelized —
  a 2000-filename call is 2000 sequential network round trips to Table
  Storage before the SAS-minting even starts. This is the same per-request
  storage engine (`azure.data.tables`) already used elsewhere with genuine
  batch support (`_batch_upsert_entities`, `app.py:3352`, capped at 100 ops)
  for writes; there's no equivalent batched *read* path used here. Worth
  profiling against real gallery page sizes before assuming it matters —
  unlike §6/§7's items, this one hasn't been measured live yet, only located
  by reading the code.

## 9. Summary of current numbers, for reference

| Metric | Value |
|---|---|
| Replicas (min / max) | 0 / 5 |
| vCPU / memory per replica | 1.25 / 2.5Gi |
| Gunicorn workers × threads | 1 × 4 |
| Worker class | `gthread` |
| Max requests per worker (recycle) | 400 ± 50 |
| Graceful timeout | 60s |
| Request timeout | 600s |
| Scale trigger | HTTP concurrency ≥ 8 concurrent requests/replica |
| Data store | Azure Table Storage (structured) + Blob Storage (media), no SQL/Cosmos |
| Listing query shape | Full `PartitionKey eq user_id` partition scan, filtered/paginated in Python |
| Per-request auth cost | 1 Table point read (+1 more for shared-library membership) |
| People/face scan cache TTL | 20s (backend) |
| Known unresolved CPU-bound risk pattern | O(library-size) Python work inline in a request handler defeats thread-based concurrency under the shared GIL — see §6 before raising `GUNICORN_THREADS` again |
| Deployed but pending | `dd2a1b1` (embedding-index cache+vectorization) — committed, not deployed |
| Uncommitted | `storage_utils.py` finalize round-trip removal (this session) |

## 10. Where to look next if optimizing further

Roughly in order of expected effort-to-payoff, based on what's already
measured vs. only located:

1. **Deploy `dd2a1b1`** — it's written, tested, and benchmarked, just not
   live yet. Free win sitting on the branch.
2. **Batch or parallelize `access-batch`'s per-filename metadata reads**
   (§8) — straightforward if it turns out to matter; needs a real
   measurement first (HAR capture or server-side timing log) since it hasn't
   been profiled against realistic gallery page sizes yet, unlike the other
   items in this doc which all have real before/after numbers.
3. **Before raising `GUNICORN_THREADS` again**, use the §6 lesson: look for
   per-request O(library-size) Python work first. The bicep comment at
   `resources.bicep:470` is effectively a standing "don't re-raise this
   without finding the CPU work" warning — the embedding-index rebuild was
   one instance of that class; there may be others in `finalize`/
   `init-batch`/`client-processing` not yet found.
4. **If replica-local caching (§5) ever becomes a hit-rate problem** — e.g.
   if `GUNICORN_WORKERS` is ever raised, or replica count regularly exceeds
   1 during normal (not just burst) load — the existing table-backed
   delegation-key pattern (§3) is the precedent for making a cache
   cross-replica without introducing a new dependency like Redis.

## 11. Large-library index path (130k photos) — instrumentation, slim index, disk cache

**Reading the logs.** Every line starting `PERF` is key=value (`perf_instrumentation.py`):
- `event=request` — `ms`, `inflight`, `rss_delta_mb`, and the top spans for that request; WARNING when `ms >= PERF_SLOW_REQUEST_MS` (1000).
- `event=mem` — every 15s: `rss_mb`, `threads`, `inflight`, `inflight_peak`. `PERF_MEMORY_WARN_MB` adds an `mem_high` warning.
- `event=span name=index.<kind>.{download,gunzip,json_parse,table_scan}` / `index.serialize` / `index.search_slim.build` — which phase of an index load/build dominates and how much RSS it adds. `index_disk_cache_hit|store` shows disk-cache behaviour.
- `PERF_INSTRUMENTATION=false` disables all of it.

**Search.** There is no browser-side search or search index any more (the 709 MB lexical blob could never load in a tab, and with server search frozen every query came back empty). See section 13.

**Indexes on the shared volume.** With `INDEX_DISK_CACHE_DIR` set (backend, tools, extras: `/mnt/photostore/shared/index-cache`; worker: `/mnt/photostore/faiss-checkpoints/index-cache` -- the same `faiss-checkpoints` Azure Files share), `_get_blob_client` returns a `_ShareBackedBlob` for the four index containers, covering every index kind (sort, lexical, listing, access, albums, people, vector, people/tag embeddings, explore, timeline); the SQLite search database is deliberately excluded (section 13). Uploads write through to the share; downloads are served from it when the blob's ETag sidecar matches (one `get_blob_properties`), else fall back to Blob and refill. Blobs under `INDEX_DISK_CACHE_MIN_BYTES` (manifests) bypass it. Blob Storage stays the source of truth and any share error falls back to it. **Session start:** `/api/photos/index-status` (called once per session) kicks `warm_user_index_files_async`, which streams every index blob for the user onto the share (single-flight, `INDEX_WARM_COOLDOWN_SECONDS`=300), without holding any in memory. Browsers cannot mount SMB, so they still download via SAS URLs from Blob (now of the slim blobs) and keep them in IndexedDB.

**Timeline.** `/photos/timeline` serves a summary precomputed on tools (`refresh_user_timeline_summary`) instead of loading the listing index into the backend.

**Client.** `preloadLocalIndexes()` starts the media token and the sort, albums and people index downloads concurrently once `/api/photos/index-status` reports ready; the search index is now persisted in IndexedDB by `sourceVersion` like the sort index.

## 12. Token-based media: thumbnails with no backend per page

**Before** (HAR, microsvcpoc-dev): every `lookup-batch` page of 100 photos signed 3 SAS URLs per photo on the backend (full image, thumbnail, thumbnail again) -- 300 distinct signatures and a ~160 KB response, and the thumbnail URLs were only available after that call.

**Now.**
- `GET /api/photos/media-token` returns ONE container-scoped, read-only, day-aligned token for the thumbnails container (`_stable_container_read_sas`); previews are in the same container under `preview/`, so it covers both. No list permission: blobs are reachable only by their unguessable UUID names. Same exposure class as the per-blob SAS URLs it replaces, but note it is a bearer token for the whole container -- rotate by rotating the user-delegation key / shortening the day-aligned window if that ever matters.
- The sort index (schema v2) carries `thumb` (physical thumbnail blob name, once the thumbnail exists) per photo.
- The browser (`services/mediaToken.ts`) caches the token in memory + localStorage until 30 min before expiry and builds `{baseUrl}/{blob}?{sas}` itself. The grid (`mockups/prototype/store.tsx` `fetchPhotos`) paints each page straight from sort-index rows + token -- no backend call -- then enriches in the background with `lookup-batch {directMedia: true}`, which skips all URL signing and returns `thumbnailBlob` instead. Proxy/preview fallbacks (thumbnail not ready, RAW/HEIC) keep their normal URLs.
- `tools` upgrades existing libraries: `/api/tools/indexes/build` runs `ensure_user_sort_index_current` (full sort-index rebuild when the stored schema is older). Until then rows lack `thumb` and the grid simply waits for the enrichment call, as before.

Known gaps: provisional tiles lack per-user `liked`, people, tags and `thumbnailRotation` until enrichment lands (normally well under a second); a tab left open past the token's expiry needs a reload to refresh already-built URLs; Albums/People/Explore/Search result grids still use their own URL sources. The Gallery now sizes each step to the screen: `measureGridCapacity()` reads the live `.pt-grid` column count / tile width and loads ~3 viewports of tiles (60-600) per step, painted straight from the sort index; only the metadata enrichment is chunked (100 per `lookup-batch`, in parallel).

## 13. Search: per-library SQLite (FTS5) database on ephemeral disk

**Why.** Search was returning nothing: the browser could not load the 709 MB index and `/photos/search` was frozen because the old implementation loaded the whole lexical index into the 1Gi backend and scored every row per query.

**Now** (`backend/search_db.py`):
- **Build (tools).** After every lexical build (`refresh_user_lexical_index`) -- and on demand via `/api/tools/indexes/build` -> `ensure_user_search_db` for libraries that predate this deploy -- tools writes `<key>-searchdb.sqlite.gz` + manifest to Blob. The DB holds a compact scorer-compatible row per photo (deduped, confidence-filtered tags; see `reduced_row` and `SEARCH_INDEX_*` env vars), an FTS5 index over the exact texts `lexical_search_score` matches against (filename, effective tags, semantic text incl. OCR, location, camera model), a `person -> photos` table, a capture-day column and the library's place-name vocabulary. ~100 MB / ~40 s for 130k synthetic photos.
- **Query (backend).** `open_database` copies the current DB to **local ephemeral disk** (`SEARCH_DB_DIR`, an EmptyDir mount; streamed, once per replica per version, oldest files evicted past `SEARCH_DB_MAX_CACHE_MB`) and queries it read-only. `/photos/search` takes the top `SEARCH_DB_CANDIDATE_LIMIT` (4000) bm25-ranked candidates (OR of query terms, their singular/plural variants, expansions, 4+ char prefixes; plus rows of named people), then runs the unchanged hard filters + `_score_search_row` on just those rows and point-reads full metadata for the returned page. Nothing library-sized is held in memory.
- **Not on the share.** SQLite needs a real local filesystem; `-searchdb` blobs bypass the Azure Files layer. `/api/photos/index-status` (session start) kicks `search_db.warm_async` so the first search doesn't pay for the download.
- **Cold library.** No current DB -> `/photos/search` returns `{photos: [], total: 0, searchIndexBuilding: true}` and nudges tools; Ask shows a "preparing" notice and retries for ~2 minutes.

**Behaviour changes to know about.** Semantic (CLIP) scoring is gone from search -- it needed an embedding model in the browser or backend; ranking is lexical + tag-embedding query expansion. Result totals are over the candidate set (capped at the limit above). The `/api/photos/search-index` route is a retired stub.

## 14. Index builds run on the worker, not inside a tools HTTP request

**The failure.** "Library index build failed — Job did not finish (worker restarted or timed out)" is the `index_build` job (one row per library, `index-build-<libraryId>`). `/api/jobs/status` rewrites any queued/running job whose row hasn't been updated for `CLUSTERING_ACTIVE_JOB_STALE_MINUTES` (15) as failed with that text. The build used to run inside `POST /api/tools/indexes/build` on the scale-to-zero `tools` app: ingress cuts requests at ~240 s, after which nothing counted as in-flight, so a scale-down (or an OOM restart of the 4Gi replica while holding the whole library in memory) killed it mid-step; and the job row was only touched *between* indexes, so one long step (full-table scan, search-database build) could trip the 15-minute sweep even while alive.

**Now.**
- `tools_build_indexes` only decides whether a build is needed (`index_build_needed`: any index missing/dirty, old sort-index schema, stale search DB) and `enqueue_index_build`s a `{type: 'index_build'}` message on the library-ops queue; it returns immediately. `_trigger_tools_index_rebuild` (backend/ipworker nudges) enqueues the same message. Status (`/api/tools/indexes/status`) reads the shared job row, not a per-process lock.
- The always-on `worker` consumes it (`_run_index_build_job`): same lease renewal, bounded retries, dead-letter queue and SIGTERM handling as library clean/download; a failed or killed build is redelivered. A heartbeat thread rewrites the job row every `INDEX_BUILD_HEARTBEAT_SECONDS` (30), so a live build is never declared dead, and a truly dead one is. The worker mounts the same Azure Files share and `INDEX_DISK_CACHE_DIR`, so indexes it builds are written through for every other role; consumers still locate them by deterministic blob name (no path hand-off needed).

**To verify after deploy:** `PERF event=span name=index.build.job` / `index.<kind>.table_scan` / `searchdb.*` show where time goes and `rss_mb` shows memory per step; if the worker still restarts mid-build, the usual cause is memory (the lexical snapshot holds every metadata column in Python objects) -- look for `event=mem` climbing toward 4Gi before the restart. No replica floors are assumed: the worker scales from the library-ops queue rule, and the outstanding (in-flight) message keeps it up.

### 14.1 Bounded-memory build (the OOM fix)

The old 'lexical' step built a full in-memory snapshot of the library (`list(query_entities)` of every column incl. the ~10 KB/photo embedding columns, a trimmed copy, a serialized 700 MB JSON, a gzip copy, a listing copy) and then Explore, timeline and the search DB each reloaded it. That is several GB of Python objects at ~130k photos.

Now `storage_utils.stream_library_artifacts` makes **one paged pass** with a server-side column projection (`_STREAM_SELECT_FIELDS` -- embeddings are never downloaded) and offers each row to small sinks, then drops it:
- `ListingSink` -- gzip-streams the listing blob to a temp file;
- `search_db.SearchDbSink` -- streams into SQLite (2000-row batches), gzips and uploads;
- `_ExploreSink` -- `ExploreAccumulator` keeps only counts + first filename per group; full metadata is point-read for the <= 2x`EXPLORE_MAX_GROUPS` winners;
- `_TimelineSink` -- `TimelineAccumulator` keeps day counters.
Sort and access scans also iterate instead of `list()`. The 700 MB full lexical blob is **no longer built** (nothing reads it any more; `get_user_listing_index` no longer falls back to building it on the backend either). Nothing is published unless the whole scan succeeds; the lexical manifest (readiness / freshness / dirty flag) is written last.

Measured on identical synthetic data (8,000 photos with 12 KB of heavy columns each): old build peak 288 MB (grows linearly with library size), streaming build peak 2.7 MB. `tests/test_library_stream.py` pins this (peak < 25 MB). Builds are a full scan, so `enqueue_index_build` delays a rebuild that follows a finished one by `INDEX_BUILD_MIN_INTERVAL_SECONDS` (120) instead of letting upload bursts trigger back-to-back builds.

### 14.2 No request path loads the library

The backend (1Gi) used to hold library-sized data in memory for several endpoints. Each now uses a bounded technique:

| Endpoint / helper | Before | Now |
|---|---|---|
| `GET /photos` (list), `GET /photos/filter` | whole listing blob (130k rows) loaded + sorted in Python | SQL `ORDER BY/LIMIT/OFFSET` on the per-library SQLite DB (`SearchDatabase.list_page` / `filter_page`); page metadata re-read fresh (parallel point reads). Ordering/filter parity with the old in-memory logic is pinned in `tests/test_library_db_routes.py` |
| `/api/suggestions` (on this day) | pass over the listing | `GROUP BY capture_year WHERE capture_md = ?` |
| `/api/search/suggest` (places) | pass over the listing | place vocabulary stored in the DB |
| album covers (`_album_cover_thumbnail_url`) | full unprojected, sorted scan of every row | `SearchDatabase.top_rated(album filenames)` (chunked `IN`, O(limit)) + <=12 point reads |
| `access-batch` | whole access index copied (130k rows) and re-indexed **per call** | one compact filename map per index version (`lookup_access_entries`), no copies; the backend never builds the index |
| trash list / restore-all | all columns of all rows, filtered in Python | server-side `processing_state eq 'deleted'` + projection |
| corrupted-uploads page | whole-library scan | server-side `verification_status eq 'failed' or corrupted eq true` + projection |
| smart-album creation, admin backfill, browser-processing pending, ipwork sweep | list of all rows | `_iter_metadata_rows_for_user` streaming with narrow projections |

`_cached_metadata_rows_for_user`, `_cached_sorted_metadata_rows_for_user`, `_cached_metadata_list_rows_for_user` and `_cached_sorted_metadata_list_rows_for_user` now **raise** (`tests/test_bounded_scans.py` also fails the build if any route module references them), so a future caller cannot silently reintroduce a whole-library load. `_invalidate_metadata_scan_cache` remains as a no-op for the many write paths that call it.

Behaviour notes: rating/like changes reach list/filter/cover ranking on the next index build (minutes); the photos returned are always fresh. With a location filter active, photos without coordinates are excluded (the old code compared them as 0,0). A library with no current database yet gets `indexBuilding: true` from list/filter and a worker build is requested. The database schema is `sqlite-v2`; existing databases are rebuilt by the next build.

Remaining library-proportional memory on the backend: the compact access map (~tens of MB per worker, loaded once per index version) and the albums/people indexes (sized by album/person count, not photo count).

### 14.3 People: no 200-cluster cap

Accounts have tens of thousands of clusters. The primary People path (the people index blob, downloaded once per session) was never capped; the cap lived in the fallback used when that index is unavailable (`listPersons(undefined, 0, 200)`), so a failed/cold index build meant only 200 people were visible.
- `GET /api/persons?namesOnly=1&covers=1` returns **every** cluster in one request -- name, `isNamed`, `faceCount` and a `coverFaceId` chosen (confirmed > confidence, never rejected) from the in-memory bulk face map: no per-person lookups and no thumbnail signing, which is the per-page work the paged endpoint does and why it pages. 30,000 clusters build in <1 s in `tests/test_person_roster.py`. Without `covers=1` the response shape is unchanged.
- `faceService.listAllPersons()` replaces the capped call in the store's fallback; covers load lazily via `/api/faces/crop/<id>`.
- People grid is windowed (`useWindowedGrid`) so only on-screen cards render and only their covers are requested (it used to request every cover); selection lookups use a Set; the merge picker on a person page is searchable and renders at most 200 options (a `<select>` with 30k options freezes the tab).

### 14.4 Index builds never run in a serving process (the `extras` OOM)

**Incident (microsvcpoc-dev-extras, 0.5 vCPU / 1Gi):** RSS climbed 289 -> 1007 MB within ~2 min of a rollout with `inflight` 1-2, was OOM-killed, and repeated until `Persistent Failure to start container`. Cause: the first People request ran the **people-index build in-process** (`get_user_people_index` -> `_rebuild_people_index_in_background`). `_build_user_people_index_snapshot` did `list(query_entities(...))` of every column of every person **and every face**, including each face's `embedding` and each person's `repEmbedding` (512-float JSON, ~6-8 KB each). Measured on identical synthetic data (distinct 6 KB embedding strings per row): old build **180.6 MB peak at 4,000 persons / 20,000 faces** (~1.35 GB extrapolated to 30,000 / 150,000), new build **12.9 MB**.

**Structural guard.** Only `worker` and `ipworker` may build indexes (`INDEX_BUILD_ROLES`, default `worker,ipworker`; `storage_utils.index_build_allowed()`). In every other process -- backend, extras, admin, upload, tools:
- the six `_rebuild_*_in_background` kickers are decorated with `_builds_only_where_allowed` and become *"request a worker build"* (`INDEX_BUILD_REQUEST_HOOK` -> `enqueue_index_build`, deduped/throttled);
- the synchronous branches of every `get_user_*_index` (sort, access, albums, people, vector, tag-embedding, people-embedding) return `None`/stale data and request a build instead of scanning;
- `_load_people_embedding_index` no longer falls back to scanning person rows (with embeddings) when the durable blob is absent.
`tests/test_index_build_guard.py` makes the person/face/metadata/embeddings tables explode on any scan in a serving process and asserts a build is requested instead.

**Builders are bounded too** (they run on the 4Gi worker, but 4Gi is also finite):
| Builder | Fix |
|---|---|
| people index | server-side column projection (no embeddings), streamed, compact face map; no thread pool per person |
| people-embedding index | projected + streamed; reps go straight to float32 arrays (not Python float lists, ~28 B/number) |
| vector index | streams the embeddings table into one normalized float32 vector per photo instead of holding every ~8 KB row JSON; narrow metadata projection; ~63 MB -> ~25 MB at 6,000 photos, result array ~2.2x |
| tag-embedding index | streams only the tag-related columns into a set |
| lexical / listing / search DB / explore / timeline | already one streamed pass (14.1) |
| sort / access | iterate instead of `list()` (14.1) |
A scan failure now aborts the build (vector/tag-embedding) instead of being persisted as an empty index.

**Light person rows.** `_cached_person_rows_for_user(user_id, with_embeddings=False)` (separate `_person_light_scan_cache`, invalidated with the others) drops `repEmbedding`; the People list/roster and the name index use it. Embedding readers keep the default.

**Remaining library-proportional memory on serving roles:** the assignment index loaded from the durable blob on the upload role (`repEmbedding` as Python lists, roughly 0.5 GB at 30k clusters -- moving per-photo assignment to the worker via `PEOPLE_ASSIGNMENT_ENGINE=faiss` removes it) and the compact access map (14.2).

### 14.5 Index builds run on disk, and uploads alone never trigger a heavy rebuild

* `backend/index_files.py` provides the disk-backed toolkit (`workspace`, `RowsWriter`/`iter_rows`, `DiskKV`). The people index keeps its face table in SQLite on disk, and the vector index streams embeddings into an `.npz` file and uploads from the file; neither holds library-sized lists in memory. `INDEX_BUILD_WORK_DIR` points the worker at the Azure Files share. SQLite scratch (`INDEX_BUILD_SQLITE_DIR`) stays on local disk.
* Per-library caches are bounded (`INDEX_CACHE_MAX_ENTRIES`), so a serving process cannot accumulate every library's index.
* Build scopes: `full` (all indexes and the search DB) and `light` (sort and access only, separate job row `index-build-<lib>-light`).
  * Dirty manifests seen after uploads, and ipworker drains, enqueue `light` builds only.
  * `full` runs when an index has never been built or the search DB is missing, after a clustering job, and after a Workbench/tools action.
  * `index_build_needed` ignores dirtiness and only reports cold, outdated-schema or missing-search-DB libraries.
* The vector index is always a full streaming pass; nothing on a serving path reads it.

The sort and access indexes follow the same rule. `_refresh_rows_index_on_disk` streams rows from the table into a gzip file and uploads from that file. It also mirrors the file to the share (`_ShareBackedBlob.upload_file`). An incremental refresh streams the previous file through and swaps in only the dirty rows. The snapshot it returns has no rows, and callers that need them reload from the blob.

## 15. Performance instrumentation (find slow steps, extra round trips, duplicate work)

Everything logs as `PERF event=...` lines (backend, worker, and the browser through `POST /api/perf/client`), so one Log Analytics query set covers the whole path. Switch off with `PERF_INSTRUMENTATION=false` on the backend and `localStorage['photostore.perf']='off'` in the browser. Query strings (SAS tokens) are never logged.

**Backend (`perf_instrumentation.py`)**
- Every Azure Table, Blob and Queue call is traced at the SDK transport. Each request and job records round-trip count, storage time, bytes and the top operations. These are labelled `table:GET:photos(..)` or `blob:GET:lexical-index`, with ids collapsed.
- `event=request` now carries `rid`, `view`, `sess`, `io_calls`, `io_ms`, `io_mb` and `io_top`. The response gets `X-Request-ID` and `Server-Timing: app;dur, storage;dur`.
- `event=dup_io` is logged when one request or job repeats the same storage call `PERF_IO_DUP_WARN` (3) or more times.
- `event=scope_summary` is logged per queue message, per ipworker message and per index-build job. It includes wall time, I/O, peak RSS, the slowest spans and `io_top`.
- `event=step` is logged per phase of a job (`index.prime.<kind>`, `index.<kind>.table_scan|fetch_previous|upload`). It reports the storage calls made inside the step. This tells a slow step apart from a chatty one.
- `event=stream_scan_split` breaks the library scan into time waiting on the table versus time in each sink, such as ListingSink or SearchDbSink.
- `event=io_totals` is logged every sample interval with process-wide storage calls ranked by time.

**Browser (`services/perf.ts`)**
- Every API call records its duration, bytes, retries and the server and storage time from `Server-Timing`. It also carries `X-Request-ID`, `X-Client-View` and `X-Client-Session`, so a browser event joins the backend line.
- Duplicates are flagged: the same request completed twice within 15 s (`event=client_dup kind=request`), concurrent identical GETs that were coalesced, and one blob fetched through several URLs or re-downloaded (`blob-multi-url`, `blob-refetch`). The last two are exactly the thumbnail double-download case.
- `event=client_view` summarises each page: requests, network time, duplicates, resources, cache hits and long tasks. `event=client_span` covers the index preload phases for sort, albums and people (manifest, IndexedDB read, blob download, parse, IndexedDB write, total, cached or not) and the media token. Web vitals are logged as `client_vital`.
- In the browser console, `photostorePerf.report()` prints slowest, chattiest and duplicate tables, plus `summary()` and `events()`.

**Starting queries**: ready-made KQL for all of the below is in `docs/perf-queries.kql` (Log Analytics, `ContainerAppConsoleLogs_CL`).
- Slowest endpoints: `event=request`, order by `ms`. Compare `io_ms` with `ms` to see whether time is storage or app work.
- Chatty endpoints: `event=request`, sort by `io_calls`.
- Duplicate storage work: `event=dup_io`, grouped by `call`.
- Slow builds: `event=scope_summary name=index.build.*`, then drill into `event=step` and `event=stream_scan_split`.
- Client versus server: join `client_req` and `request` on `rid`. `ms - serverMs` is network plus queueing.
- Duplicate downloads: `event=client_dup`, grouped by `kind`.

### 15.1 Fixes made from the first production PERF data

- **Job status:** `/api/jobs/status` filters server-side (in-flight or recently updated). Finished rows older than `JOB_RETENTION_DAYS` (14) are swept in the background, at most hourly per user (`event=job_sweep`).
- **Naming a person:** faces are confirmed with parallel reads and 100-row write transactions. Each step logs `event=step name=label.*`.
- **Smart albums:** read from the library's local search database (`SearchDatabase.iter_smart_rows`) when a current one exists, and fall back to the table scan otherwise. Groups are the same as the table scan's, except tags: the database keeps only the high-confidence tags.
- **Parallel partition scan (`table_scan.py`):** the library scan reads RowKey ranges concurrently and still returns rows in RowKey order. Hot prefixes (`IMG_...`) are split on the fly. Settings: `TABLE_SCAN_PARALLELISM` (default 4, 1 = off), `TABLE_SCAN_SPLIT_ROWS`, `TABLE_SCAN_QUEUE_ROWS`.
  - `event=table_scan` logs rows, ranges and ms. Compare ms per 1000 rows with `TABLE_SCAN_PARALLELISM=1`.
  - Azure documents a soft target of about 2,000 entities per second per partition, so expect a gain of up to roughly 2x. Watch for 503s on the table when raising the worker count.
- **Thumbnails:** every grid builds its thumbnail URL from the sort index's blob name and the single container token. A thumbnail loaded on one page is then a browser-cache hit on the others. Album covers use the same path.
- **Duplicate-call detection:** table queries are keyed by a hash of their `$filter`, so `dup_io` means the same query was repeated.
- **Not changed:** `finalize-batch` still makes about 12 storage calls per file. That would need a batched finalize across files.

### 15.2 Fixes from the browser smoke test

- **Trash:** membership comes from a small trash index table (`TRASH_INDEX_TABLE`, default `phototrashindex`; PartitionKey = library, RowKey = filename). It is maintained on soft delete, restore and purge. Before this, "list trash" was a server-side filtered scan of the whole library, about 45 s at 130k photos even with an empty trash.
  - The first call per library builds the index with one scan, then later calls are a query over only the trashed rows.
  - The listing reads just the requested page fresh and prunes stale index entries.
- **Gallery pages:** a page's rows are fetched 15 per query (`RowKey eq .. or ..`), in parallel, instead of one point read each. This is about 4 round trips instead of 48.
- **People:** the face and person scans use the parallel scan. Merge suggestions read only named people instead of every cluster's embedding, which was both minutes of work and a memory risk on the extras app.
- **Smoke script:** it now retries cold-start gateway errors, warms all three apps first and reports cold-start times separately. It decompresses blobs only when the bytes start with the gzip magic number. The earlier "Failed to fetch" on the index blobs and thumbnails was this script bug, not the app.

### 15.3 Delete and merge paths, reviewed for the same patterns

The smoke test did not call destructive routes. These were read for the patterns it exposed: full-table scans per call, one-at-a-time storage calls, and heavy work inside the request.

| Route | Problem found | Change |
| --- | --- | --- |
| `POST /api/persons/<id>/merge` and `/merge/batch` | `_merge_persons_core` scanned the whole face table for every pair, so a 50-pair batch did 50 full scans. It also read every moved face one at a time. | One shared snapshot per batch, parallel face reads, and parallel member-row writes. |
| `POST /api/persons/<id>/label` | Identity propagation, a full face-table scan, ran inline. | It is queued to the worker like merge, with the inline path only as a fallback. The response now carries `propagateJobId`. |
| `POST /api/persons/merge/<id>/undo` | Sequential per-face reads and writes. | Parallel. |
| `POST /api/persons/delete` and `/<id>/delete` | Per-face sequential reads and writes, and clusters deleted one after another. | Faces and clusters are released in parallel. |
| `POST /api/photos/trash/purge` (hard delete) | Read every cluster's embeddings while reconciling people. It also scanned the whole library in the request to clean stale `peopleIds`. | Projected columns only, parallel scan, and the library-wide cleanup runs in a background thread. |
| `POST /api/albums/<id>/photos/add` | One sequential point read per photo to check it exists. | Batched queries, about 4 round trips per 50 photos. |
| `POST /api/photos/delete` and restore | Already parallel point reads and writes. | Trash-index update per photo only. |

Known remaining cost: purge's job-row cleanup still reads the user's jobs partition. The retention sweep (§15.1) keeps that small.

### 15.4 Incremental search database (no full rebuilds for normal change)

A full library rebuild is minutes at 130k photos and does not scale to millions, so ordinary change is now applied as small **deltas**.

- **Worker (`refresh_user_search_db_incremental`):** reads the changed filenames from the `lexical` dirty table and fetches just those rows (15 per query, in parallel). It publishes a delta blob `<key>-searchdelta-NNNNNN.json.gz` of upserted photos and removed filenames, then advances `deltaSeq` in the manifest with a compare-and-swap on its ETag. A concurrent full rebuild therefore cannot be overwritten. Large changes go out in chunks of 5000. A dirty-table outage reports `unavailable`; it is never read as "nothing changed".
- **Replicas (`search_db.sync_deltas`):** `open_database` compares the manifest's `deltaSeq` with the local copy's `delta_seq` and applies the missing deltas in place, in one WAL transaction each, so readers never block. A joining replica downloads the base file once, then applies the deltas. An updated photo's old FTS entry is removed by re-deriving the exact indexed document. If a delta can't be fetched, the replica keeps serving what it has and retries on a later request.
- **Compaction:** one full rebuild (`prime ... kinds=('lexical',)`) runs only when the delta log reaches `SEARCH_DB_DELTA_MAX_COUNT` (200), the deltas cover more than `SEARCH_DB_DELTA_MAX_ROW_FRACTION` (25%) of the base, or more than `SEARCH_DB_DELTA_MAX_NAMES` changed at once. A new base resets `deltaSeq` and deletes the old deltas.
- **Timeline** is computed from the replica's database (`timeline_summary`, cached until the next delta), so it reflects new uploads immediately.
- **Manifest reads** are cached for 5 s (`SEARCH_DB_MANIFEST_TTL_SECONDS`) instead of one storage call per request.

Build scopes and triggers:

| Scope | Runs | Triggered by |
| --- | --- | --- |
| `full` | everything | library never indexed, schema upgrade, compaction request |
| `light` | sort + access indexes, then the database delta | ipworker queue drain / every 10k files, dirty sort/access observed, Workbench action |
| `people` | people + albums indexes, then the database delta | a clustering, recluster or propagate job finishing |

Each scope has its own job row and trigger cooldown. `light` and `people` touch different indexes, so ipworker and clustering do not repeat each other's work; whichever runs second finds the dirty set empty and its delta step is a no-op. `people` requests wait at least `INDEX_BUILD_PEOPLE_MIN_INTERVAL_SECONDS` (600) after the previous one.

Still full-rebuild-only: the Explore places/things summary. Still O(library) per light build: the sort and access index files (streamed on the worker, but a million-photo sort index is too big for the browser either way).

### 15.5 Indexing a 1M-photo library

**Targets**

| Target | How it is met |
| --- | --- |
| A first build can finish despite restarts, deploys and scale events | `library_build.bootstrap_library_build` is chunked and resumable. The cursor and the delta publish are one atomic manifest update, so a restart loses at most one chunk (20,000 photos). |
| Search is usable long before the build ends | An empty base database is published first and every chunk is appended as a delta. Responses carry `indexPartial: true` until the build finishes. |
| One table scan, not four | The same pass produces the search rows, the sort-index rows and the access-index rows (spooled per chunk, then assembled). Before this it was a scan for the sort index, another for access, another for search, and the listing blob had no consumer. |
| Memory is bounded | O(chunk), about 20-40 MB. |
| Disk is bounded and checked | The worker holds its local database copy plus one snapshot during compaction, and compaction refuses to start without enough free disk (2.5x the database + 64 MB). The spool is removed when the build ends. |
| No repeated full rebuilds | Normal change is deltas (§15.4). Compaction folds the delta log into a new base from the worker's local copy and never reads the table. The scan build runs only for a library with no usable database (new, or a schema upgrade). |
| The first build does not starve live traffic | `LIBRARY_BUILD_MAX_ROWS_PER_SECOND` throttles the scan (0 = unlimited, the default). |

**Compatibility:** `deltaSeq` is global to a lineage; compaction changes the base's `sourceVersion` and `baseSeq` but not the lineage. Readiness (`search_db.is_current`) compares lineages, so a compaction does not make the library look "stale".

**Summaries:** Explore and the timeline are recomputed from the finished database after a first build and after each compaction (`_finalize_library_summaries`), not from another scan.

**Operating it:** PERF events `library_build_started|resumed|progress|done`, `searchdb_delta_published`, `searchdb_compacted` (queries 13-14 in `docs/perf-queries.kql`).

**Known limits at 1M, not yet addressed**
- **Backend disk:** each backend replica keeps a local copy of the library's database. The 0.5 vCPU backend has about 2 GiB of ephemeral disk and `SEARCH_DB_MAX_CACHE_MB` defaults to 1200. Read `sqlite_mb` from the `searchdb_built` / `searchdb_compact` log lines at 130k and multiply by 7.7. If the result is above about 1.5 GB, give the backend 1 vCPU (4 GiB ephemeral) and raise the cache budget.
- **Browser sort index:** about 24 MB at 130k photos, so about 180 MB at 1M. The browser cannot download that; the gallery needs server-side paging first.
- **People and albums indexes** still read every cluster and face for each build.
- **Sort/access light builds** rewrite their whole file when something changes (a streamed disk pass, about a minute at 1M).

**Measured on a synthetic 1,000,000-photo library** (one worker core, in-memory fake storage, so no network time): the chunked build took 410 s, throughput was flat at about 3,300 photos/s from the first chunk to the last (no super-linear step), peak RSS was 374 MB, and the finished database was 599 MB (75 MB gzipped), with the sort and access indexes at 7 MB and 6 MB gzipped. A search over the whole library took about 0.4 s. In production the table scan (about 1,000 photos/s per sequential read stream) is what dominates, so expect 15-20 minutes at 1M, resumable throughout. The scan was the only thing that was super-linear before. Gallery ordering now uses plain indexed column order, and the upload-time index was added: page 1 went from 124-380 ms to under 1 ms at 1M rows, and a 500k offset from 3.7 s to 1.2 s.

### 15.6 Albums and the gallery at a million photos

**Albums index** (schema `v2`)
- Covers are chosen by SQL over the library database (`top_rated`), one lookup per album. Building the index no longer loads every photo's sort row into a dict, which was hundreds of MB at 1M photos.
- The index no longer carries each album's filename list. Rows are `{albumId, name, photoCount, coverFilename, updatedAt, share fields}`, so the download grows with the number of albums, not their size.
- `GET /api/albums/<id>` serves an album's contents from the server: photo rows are read in batches of 15 (it was one point read per photo) and `?offset=&limit=` returns a window (`total`, `hasMore`). The browser fetches the first 120 photos, shows them, and streams the rest.

**Album size cap (a bug I found).** An album's photo list was one JSON string in a single Table property, which Azure caps at 64 KB, so an album could hold only about 1,500 photos and a larger write failed with a 400. This is likely why smart albums on a big library "didn't work". `album_store.py` now splits the list across `filenames`, `filenames_1` ... `filenames_13`, giving roughly 14,000-20,000 photos per album (about 784 KB of the 1 MB row limit), with the same single row, point read and transaction. Existing rows read back unchanged. An add beyond the limit returns 413 `album_too_large`. A smart album larger than the limit is trimmed to what fits, with a message.

**Gallery paging.** The sort index is a JSON file of every photo, about 24 MB at 130k and about 180 MB at 1M, which a browser can't download and sort. `/api/photos/sort-index` now answers `{available: false, reason: 'library_too_large', rowCount}` above `SORT_INDEX_CLIENT_MAX_ROWS` (200,000), so the browser downloads nothing and does not retry, and the gallery pages from the server (`/api/photos?sort=capture&offset&limit&directMedia=1`).
- A server page is one indexed SQL page over the library database plus four batched row reads, so it costs the same at 10k or 10M photos. Page size follows the screen.
- For those libraries the sort index is no longer built, marked dirty or rewritten by light builds (`sort_index_skipped`). Readiness still holds, via a manifest with `skipped: true`.
- Rating and like edits used to dirty only the sort index, so the search database's rating column went stale. They now also mark the photo for the next delta.

### 15.7 No result caps: search, people, Workbench, trash

The old 200-result limits are gone. Each list now pages, reports an exact total, and keeps the DOM small by infinite scroll.

- **Search.** Ranking runs over a window of the best 4,000 candidates (cached 45s); pages are cut from that window. Matches beyond the window are appended newest-first, so every match is reachable exactly once. `total` is an exact SQL count, `rankedWindow` says how many are ranked, and the UI notes that later results are in date order.
- **Thumbnails for 200K results.** One container-scoped media token covers every thumbnail. The page returns `directMedia` blob names, the browser builds URLs locally and only fetches the tiles that scroll into view. Result size affects paging and selection, not tokens.
- **People.** `GET /people/<id>` pages (`offset`, `limit`, `total`, `hasMore`), best faces first, reading the cached face summary instead of the cluster.
- **Workbench.** The grid lists from `/api/photos` (`sort=date|name`, `nameContains`, infinite scroll). "Select all (N)" asks the server for matching filenames with `idsOnly=1` (5,000 per call, up to 100,000 selected, with a note if more match), so selection does not depend on what has loaded. Deep-linked photos are pinned to the top.
- **Trash.** The list pages through every trashed photo, 200 per request.
- `lookup-batch` still takes at most 200 filenames per call; callers chunk, so it is a batch size rather than a result cap.

### 15.8 People index refresh and big albums

- **People index.** A refresh no longer reads every face. After the first full build, the manifest records `builtThrough` (start of the build minus 3 minutes). The next refresh queries only persons and faces written since then (`Timestamp ge ...`), re-derives just those clusters with point reads, drops clusters no longer in the person table (a key-only pass), and renumbers "Unnamed N". Clusters whose cover face was touched are re-derived too, which covers a face that moved without its old cluster's row being rewritten. A full build also runs at least every 24 hours (`PEOPLE_INDEX_FULL_REBUILD_HOURS`), so any drift is bounded. It falls back to the full build when the previous snapshot is missing or stale, the schema changed, or more than `PEOPLE_INDEX_INCREMENTAL_MAX_CHANGED` (3,000) clusters changed. Schema is now `v2`, so each library does one full build on first refresh after deploy.
- **Albums over about 14,000 photos.** When an album's list no longer fits in its row it moves to the `photoalbummembers` table (PartitionKey = album id, RowKey = filename). The album row keeps `storage='table'` and `photoCount`. Small albums are unchanged. Add/remove touch only the changed rows (batches of 100), counts stay exact, `GET /albums/<id>` pages by walking RowKeys, and share-view membership checks are point reads. Table-backed albums list in filename order and their payload has `filenames: []` with `membersPaged: true`. The table is created at startup like the other tables. Without it, oversized albums still return `album_too_large`.
