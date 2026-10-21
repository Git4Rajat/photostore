# Index maintenance migration

## Scope of this stage

Implemented: scoped scheduling, recoverable intents, revision-aware
acknowledgements, dedicated catalog ownership, lightweight naming and isolated
recovery. This does **not** claim the deferred 1M-record contract: whole snapshot
merges/writes and cold database downloads still depend on library size.

## Mutation dependencies

| Change | Scheduled work | Not scheduled by that change |
| --- | --- | --- |
| Rename an already named person | People; current names joined during search | Discovery, representative recalculation, photo fan-out, Sort, Albums, duplicate check, full bootstrap |
| First name or removal of a name | People; background sync of that person's legacy photo membership, Search/Sort where membership changes | Discovery, duplicate check, unrelated full build |
| Unchanged label/rating | None, except relaying an existing pending intent | New revision for an identical saved value |
| Rating/likes | Search, Sort, album cover selection | Media access, clustering, duplicate check |
| Geocoded location | Search | Sort, media access, duplicate check |
| Preview/thumbnail readiness | Media access and actual dependent catalog fields | Discovery, duplicate check |
| New processed faces | Per-library assignment batch | Full DBSCAN just because a person was named |
| Upload hashes | Explicit duplicate-check scope, upload-quiet delay | General index completion does not rebuild hashes |

Dependencies follow actual projection fields in `metadata_index_kinds`. Some
legacy processing/EXIF fields conservatively affect several projections.
Tag embeddings remain model-owned by Vision; the lightweight indexer must not
publish incompatible hash-fallback embeddings. The legacy photo vector artifact
remains on-demand. These artifacts are invalidated, not eagerly queued for every
metadata edit.

## Durable work

- `index_updates.py` validates scopes. Unknown scopes cannot become full builds.
  Read hooks retain the kind. Tools auto mode schedules dirty/missing kinds,
  reserving full bootstrap for a genuinely missing Search base. Routine jobs
  do not reconcile the library or refresh duplicate hashes.
- `index_work.py` stores one intent per library/scope in the existing jobs
  table's separate `index-maintenance-pending` partition. Every actual edit
  advances a revision even when its wakeup coalesces. Read polls do not.
  Completing an older revision preserves and schedules the newer one.
- Queue messages are wakeups. Work uses a renewable 180-second owner lease.
  Recovery retries expired wake reservations, reading at most 100 eligible rows
  per pass. Server-side filtering excludes leased/reserved/failed rows before
  that limit, so busy libraries cannot occupy the entire recovery page.
- Dirty acknowledgements delete only captured ETags, batching up to 100 deletes.
  Concurrent re-marks survive. Failed flushes retain every unsent filename.
  A transient dirty-state read does not justify a full Sort/Access scan.
- Participating blob publishers check the work lease and use conditional writes.
  Swallowed publication errors still prevent final acknowledgement. Existing
  Search CAS is preserved. This is not an atomic source/index transaction or
  a replacement for immutable chunked artifacts.
- The two central metadata update helpers save `indexPending` in the source
  write, then relay dirty records and durable queue intents. Labels similarly
  retain `labelPending` and `labelMembershipPending`. Only matching tokens/ETags
  are cleared. Other direct-upsert paths retain existing publication behavior;
  this does not migrate every source writer.
- A source commit followed by failure before ledger creation remains flagged
  on its source row. **Explicit reconciliation** scans and relays these flags.
  There is no always-on fleet-wide source-outbox relay. Automatic ledger
  recovery runs at worker startup and every 60 seconds while alive. A queue
  outage with no worker running can require an explicit recovery wake.
- Retries are bounded at five per intent revision, also at the queue layer.
  Exhausted revisions stay failed until an explicit retry/new edit. Old queue
  failure cannot poison a newer revision. Completed duplicate wakeups are
  no-ops. Read polls do not restart poison revisions.

## Ownership and deployment

`dedicatedIndexWorker=true` in Bicep configures:

- `INDEX_MAINTENANCE_QUEUE_NAME=photostore-index-maintenance` on producers and
  `INDEX_BUILD_ROLES=index-worker` for generic catalog builders.
- Two containers in the existing indexer app: a 0.5 CPU/1 GiB Tools API and a
  1.5 CPU/3 GiB queue consumer using the same core image. No ninth app; combined
  allocation stays 2 CPU/4 GiB.
- Managed-identity queue scaling (visible messages only), min replicas 0,
  max replicas 1, shutdown grace 180 seconds. One publisher is deliberate
  during legacy artifact migration. Library work keys are isolated, but jobs
  execute serially: no parallel-publisher throughput/fair-latency guarantee.
- New catalog jobs use the indexer queue. Cluster durably forwards old
  `index_build` messages without executing them in dedicated mode. Face
  assignment/discovery remain clustering work; model-dependent tags stay Vision.

`APP_ROLE=index-worker` starts the queue consumer without public route groups.
The API sidecar serves Tools and job-status requests.

`dedicatedIndexWorker=false` restores legacy ownership. Drain/inspect dedicated
queue and ledger and relay source flags before rollback: disabling the consumer
does not transfer outstanding messages. Roll out producers and consumer
together; retain the cluster bridge until old messages drain. Do not increase
publisher replicas without auditing remaining legacy/shared artifact writers.

## Batching and recovery APIs

- Face batches flush at 1,500 detected faces (not filenames), five minutes from
  the oldest pending photo, manual request or shutdown. Repeated results for a
  buffered photo do not inflate counts. These limits are per library **per
  Vision replica**, not a distributed global counter.
- Messages remain bounded to 128 filenames. Partial/failed sends retain batches
  and checkpoints for idempotent replay. Checkpoints include in-flight sends;
  success cannot erase newer buffered photos. Checkpoints are best effort,
  normally refreshed every 15 seconds: a hard kill before the first checkpoint
  can still lose memory-only progress. This is not a transactional face outbox.
- `POST /api/tools/indexes/recover-pending`, optional body
  `{"retryFailed": true}`, resets failed **ledger** revisions. Restricted to the
  authenticated library; does not scan source tables.
- `POST /api/tools/indexes/maintenance/reconcile` explicitly scans catalog and
  person source flags, repairs missing records and schedules catch-up.
- `POST /api/tools/indexes/maintenance/compact` compacts Search independently.
  Ordinary deltas hitting capacity schedule it, then separate catch-up, instead
  of inline compaction. Missing-base repair uses bootstrap. Deferred Search
  notifications do not claim the index is current.
- `/api/tools/indexes/rebuild/<kind>` retains exact scope. Queued index jobs do
  not expire as crashed merely for waiting. Running jobs use heartbeat and
  scope-specific timeouts. Tools recognizes scoped jobs, not just full builds.

## Deferred work and validation

Scale work still includes chunked/delta People/Albums/Sort/Access/vector/hash
publication, normalized membership for all consumers, paginated cold reads,
distributed fairness and 1M-record multi-user benchmarks. Naming still merges
the People snapshot; first naming/removal still synchronizes legacy membership.

Tests cover required/forbidden scopes, concurrent edits, ETag re-marks, queue
and publication failures, recovery isolation, naming and batch deadlines.
Bicep compilation is local template checking, not live Azure validation.
No live resources were changed. Deployed behavior and cost require rollout
verification with actual logs.
