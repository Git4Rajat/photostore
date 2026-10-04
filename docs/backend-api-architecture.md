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

**Slim search index (schema v2).** The browser never gets the full lexical blob (709 MB JSON at ~130k photos). `refresh_user_lexical_index` also writes `<key>-search.json.gz` + manifest, built by `_search_slim_row`: tags from `tags`/`objects`/`backgroundTags` are merged into one deduped array (`subjectTags` kept separately, as semantic text uses it) and shipped as real arrays instead of JSON-in-JSON strings; AI tags below `SEARCH_INDEX_TAG_MIN_CONFIDENCE` (0.45; user/stored tags always stay) are dropped and each photo is capped at `SEARCH_INDEX_MAX_TAGS` (20); raw AI-vision predictions (up to 160/photo) become at most `SEARCH_INDEX_MAX_PREDICTIONS` (8) bare labels scoring >= `SEARCH_INDEX_PREDICTION_MIN_SCORE` (0.5); lat/lon become a presence flag; the three upload-date fields collapse to one; EXIF keeps only the keys search reads; OCR is whitespace-collapsed and capped at `SEARCH_INDEX_OCR_MAX_CHARS` (400). `/api/photos/search-index` serves only a *current* slim blob (manifest sourceVersion + schemaVersion match); otherwise it returns `available:false` and nudges tools, whose `/api/tools/indexes/build` calls `ensure_user_search_slim_index` to derive it from the existing lexical blob (so a deploy needs no full rebuild). The full lexical blob stays the server-side source of truth.

**Indexes on the shared volume.** With `INDEX_DISK_CACHE_DIR` set (backend, tools, extras: `/mnt/photostore/shared/index-cache`; worker: `/mnt/photostore/faiss-checkpoints/index-cache` -- the same `faiss-checkpoints` Azure Files share), `_get_blob_client` returns a `_ShareBackedBlob` for the four index containers, covering every index kind (sort, lexical, listing, slim search, access, albums, people, vector, people/tag embeddings, explore, timeline). Uploads write through to the share; downloads are served from it when the blob's ETag sidecar matches (one `get_blob_properties`), else fall back to Blob and refill. Blobs under `INDEX_DISK_CACHE_MIN_BYTES` (manifests) bypass it. Blob Storage stays the source of truth and any share error falls back to it. **Session start:** `/api/photos/index-status` (called once per session) kicks `warm_user_index_files_async`, which streams every index blob for the user onto the share (single-flight, `INDEX_WARM_COOLDOWN_SECONDS`=300), without holding any in memory. Browsers cannot mount SMB, so they still download via SAS URLs from Blob (now of the slim blobs) and keep them in IndexedDB.

**Timeline.** `/photos/timeline` serves a summary precomputed on tools (`refresh_user_timeline_summary`) instead of loading the listing index into the backend.

**Client.** `preloadLocalIndexes()` starts the sort, albums, people and search index downloads concurrently once `/api/photos/index-status` reports ready; the search index is now persisted in IndexedDB by `sourceVersion` like the sort index.

## 12. Token-based media: thumbnails with no backend per page

**Before** (HAR, microsvcpoc-dev): every `lookup-batch` page of 100 photos signed 3 SAS URLs per photo on the backend (full image, thumbnail, thumbnail again) -- 300 distinct signatures and a ~160 KB response, and the thumbnail URLs were only available after that call.

**Now.**
- `GET /api/photos/media-token` returns ONE container-scoped, read-only, day-aligned token for the thumbnails container (`_stable_container_read_sas`); previews are in the same container under `preview/`, so it covers both. No list permission: blobs are reachable only by their unguessable UUID names. Same exposure class as the per-blob SAS URLs it replaces, but note it is a bearer token for the whole container -- rotate by rotating the user-delegation key / shortening the day-aligned window if that ever matters.
- The sort index (schema v2) carries `thumb` (physical thumbnail blob name, once the thumbnail exists) per photo.
- The browser (`services/mediaToken.ts`) caches the token in memory + localStorage until 30 min before expiry and builds `{baseUrl}/{blob}?{sas}` itself. The grid (`mockups/prototype/store.tsx` `fetchPhotos`) paints each page straight from sort-index rows + token -- no backend call -- then enriches in the background with `lookup-batch {directMedia: true}`, which skips all URL signing and returns `thumbnailBlob` instead. Proxy/preview fallbacks (thumbnail not ready, RAW/HEIC) keep their normal URLs.
- `tools` upgrades existing libraries: `/api/tools/indexes/build` runs `ensure_user_sort_index_current` (full sort-index rebuild when the stored schema is older). Until then rows lack `thumb` and the grid simply waits for the enrichment call, as before.

Known gaps: provisional tiles lack per-user `liked`, people, tags and `thumbnailRotation` until enrichment lands (normally well under a second); a tab left open past the token's expiry needs a reload to refresh already-built URLs; Albums/People/Explore/Search result grids still use their own URL sources.
