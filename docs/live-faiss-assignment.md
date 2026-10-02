# Live FAISS assignment

Incremental face assignment now defaults to `PEOPLE_ASSIGNMENT_ENGINE=faiss`.
The existing `legacy` engine is an explicit rollback option; FAISS errors do
not silently fall back to library-wide linear scans.

## Data path

1. Acquire and renew one Blob lease per library in the existing people index
   container. Busy/lost leases leave queue work retryable.
2. Restore a matching durable checkpoint to local disk, or cold-build from
   ordered, paginated face and embedding Table scans.
   Normalized exact float32 vectors and ownership live in local SQLite, not
   library-sized Python embedding lists.
3. Query compatibility-separated FAISS indexes (alignment, version, dimension).
   Small collections use exact flat search; larger collections use IVF-PQ.
4. Retrieve candidates from the compressed base and bounded exact delta.
   Exact-rerank retrieved vectors and compare the best two **distinct people**.
   Fresh point reads reject deleted/rejected faces and missing people.
5. Persist person, membership and face ownership, then append to the local
   delta. Deterministic new-person IDs support interrupted-write recovery.
   Conditional Table updates protect entities carrying ETags from concurrent
   curation. Metadata projection uses per-photo reads.
6. Compact saturated deltas from local SQLite, not a fresh cloud scan. Uploads
   no longer automatically enqueue full-library DBSCAN maintenance in this mode.
   Explicit repair/recluster operations retain the old implementation.

## Configuration

| Variable | Default |
| --- | --- |
| `PEOPLE_ASSIGNMENT_ENGINE` | `faiss` |
| `PEOPLE_FAISS_CANDIDATES` | `64` |
| `PEOPLE_FAISS_NPROBE` | `64` |
| `PEOPLE_FAISS_DELTA_LIMIT` | `10000` |
| `PEOPLE_FAISS_THREADS` | `2` (one on macOS) |
| `PEOPLE_FAISS_MEMORY_BUDGET_BYTES` | `2147483648` |
| `PEOPLE_FAISS_WORK_DIR` | system temporary directory; worker deployment uses local EmptyDir |
| `PEOPLE_FAISS_CHECKPOINT_DIR` | disabled unless set; worker deployment uses the Azure Files mount |
| `PEOPLE_FAISS_CHECKPOINT_INTERVAL_SECONDS` | `300`; `0` saves every completed nonempty assignment batch |
| `CLUSTERING_WORKER_BATCH_SIZE` | `1` (opt-in; start with `8`, maximum `32`) |
| `PEOPLE_FAISS_IO_CONCURRENCY` | `4` (maximum `16`) |
| `PEOPLE_FAISS_COALESCE_WRITES` | `false`; experimental staged persistence/projection path requires explicit `true` |

Existing assignment threshold/margin settings still apply. The backend/worker
Docker image installs the separate pinned FAISS requirements. The lease and
revision blobs reuse the existing managed-identity Blob client and container;
no resources or credentials are provisioned by the runtime.

## Throughput microbatches

Measured production samples found metadata projection dominated real-assignment
latency (~96%), while person/member/face persistence accounted for ~1.3%.
The primary optimization now runs **inside** each metadata projection: fetch
indexed face rows concurrently, then fetch each distinct person concurrently,
using `PEOPLE_FAISS_IO_CONCURRENCY` (default 4). It applies to single-message
processing, ordinary lease-sharing microbatches, and owned-face projection retries.
It does not require staged writes. Face payloads retain RowKey order and named
people retain first-face order; only missing entities are skipped, transport
errors propagate before metadata is written. Pending reads use a worker-sized
window, not one future per library entity. A missing filename index retains the
bounded filename-query fallback. Logs break out `face_read_ms`, `person_read_ms`
and `write_ms`, in addition to the assignment's total `metadata_ms`.
`face_read_ms` includes filename-index lookup, so it is not proof of slow face
point reads. The additional `faiss metadata face retrieval` log separates
`lookup_ms`, `fallback_query_ms` (full iterator enumeration), and
`face_point_reads_ms`, with `path=filename_query|indexed_point_reads` and
`lookup_ids` (-1 means unavailable). Storage lookup logs explain fallback:
`missing_row`, `client_unavailable`, `schema_mismatch`, `state_writing|state_dirty`,
`invalid_generation`, `lease_not_cleared`, or `invalid_face_ids`.
These diagnostics do not weaken completeness checks or repair rows.
An Azure Table filename property filter is not a secondary index; missing lookup
coverage can require scanning the library partition even for a one-face photo.
Check coverage before increasing thread count. Backfills must use the existing
generation/lease/CAS protocol, not publish a query result as complete while
uploads or curation can change the filename's faces.
Assignment logs also distinguish `newly_assigned_faces` from `already_owned_faces`;
filter for the former greater than zero when assessing new-assignment latency.
This count includes new face ownership assigned to an existing person, not just
newly created person identities. Projection-only retries have projection logs
but need no assignment call. In staged mode these counts are provisional until
the batch-flush success log.

Staged write coalescing described below remains experimental and **disabled by
default** because measured persistence cost does not justify enabling that larger
redesign yet. Re-measure with genuinely new assignments: throughput dominated by
already-owned-face backlog does not establish the 10,000 new photos/hour target.

Set `CLUSTERING_WORKER_BATCH_SIZE=8` to receive up to eight messages per queue
request without waiting to fill a batch. Adjacent live incremental messages
for the same library share one assignment Blob lease and one pre-write durable
revision publication. Other libraries, maintenance, malformed messages and
exhausted retries retain the individual dispatch/dead-letter path. Default 1
retains the original worker behavior for staged rollout.

Per-photo metadata/face-ID preparation and source face/embedding point reads
overlap with bounded I/O threads. Source prefetch retains at most 128 faces;
additional faces use fresh sequential reads. Identity decisions and local delta
updates stay ordered. With coalescing enabled, later jobs see earlier **pending**
exemplars in a private runtime protected by the process lock; the pending-face
validator uses those staged records, while other candidates still get fresh
cloud validation. No checkpoint publishes pending exemplars. Duplicate source
IDs use staged ownership and unique entity operations. ETags are retained on
prefetched faces and the original read of each person. Failed prefetch starts
no Table writes.

Flush order is person transactions, member transactions, then face transactions.
Repeated person membership additions coalesce into one update per person.
New people use conditional creates. Membership is grouped by person partition;
person/face updates are grouped by library partition. Transactions contain at
most 100 unique entities with a conservative 2 MiB estimated payload cap, below
the 4 MiB service limit. The tables/partitions are NOT one atomic transaction.
Any persistence/local-index failure invalidates speculative runtime state and
retains the entire group's messages, even if some cloud writes committed.
Deterministic IDs and membership/face retries recover those partial outcomes.
Groups over 256 unique input faces fall back to the original sequential path;
staging itself rejects overflow rather than growing without bound.

After persistence, distinct filenames project metadata with up to
`PEOPLE_FAISS_IO_CONCURRENCY` concurrent calls. Duplicate filenames project once.
A projection failure retains only that filename's messages; its durable face
ownership remains available for retry. Owned-face retries project metadata even
without opening an index. A group acknowledges nothing before all transactions,
projections and lease checks finish. The pending-message renewer covers the whole
staged group and acknowledgement uses the newest receipt. With coalescing disabled,
the original per-message processor still handles renewal and acknowledgement.
The priority library-ops queue is checked between microbatches, not between
every photo in a batch; shutdown drains the already-received bounded group.

Logs include batch `lease_ms`, `prefetch_ms`, face count and elapsed time.
The `faiss batch flush` record reports entity counts, transaction count,
`persistence_ms`, `metadata_ms` and metadata failure count; staged per-photo
assignment logs measure decisions, not deferred flush latency.
Measure jobs/hour with a sustained backlog and compare against batch size 1.
Single-message intake or trickling arrivals cannot amortize the lease cost.
Membership for eight different people still needs eight partition transactions;
do not expect one member transaction for every group. No throughput multiplier
or million-face capacity is certified by unit tests. No deployment is performed
by changing these defaults; batch size remains opt-in unless configured externally.

External person/face mutations publish source revisions; ownership transfer or
revision changes invalidate the local snapshot. New unowned upload faces do
not invalidate the assigned-face index. Assignment writes bypass those hooks
and update the bounded delta directly.

## Durable checkpoint recovery

The adapter now calls checkpoint restore before building. Per-library paths are
SHA256 hashes of library IDs. Active SQLite and restored native indexes stay on
local disk; only closed snapshot files are streamed to the trusted mounted share.
Matching snapshots load SQLite and native indexes without cloud scans or training.
Missing, incompatible or corrupt snapshots fall back to cold build; share I/O
outages are logged and fall back. Blob revision/lease errors still fail closed.

Snapshot freshness includes **both** the external curation revision and a durable
assignment revision. The latter is published to the leased lock blob BEFORE the
batch's first Table write, including writes that may commit then return an error.
It survives ownership transfers, unlike the process/cache ownership token. Thus
a restart cannot silently accept a snapshot preceding newer raw assignment writes.
Lease loss prevents normal completion. Snapshots retain the acquired curation
revision, never a newer token from an edit the local runtime has not applied.

After successful persistence and metadata projection, the adapter saves the first
snapshot and then rate-limits saves to the configured interval. **This is not a
durable delta journal.** If assignments committed since the last snapshot, that
snapshot is rejected on restart and cloud rebuild is required. Setting the
interval to 0 removes the completed-batch window but copies the whole SQLite/base/
delta snapshot after every batch; this is NOT suitable for the million-face,
10,000-photos/hour target. Keep the warm replica floor while a durable incremental
journal and measured recovery/throughput work remain outstanding.

Immutable generations are not automatically pruned: provision share capacity
and monitor usage. Failed publication can leave an unused generation. Cleanup
must preserve readers' generations; do not delete them during active restore.
Old worker images do not understand the extended lease-state schema: do not
mix old and checkpoint-aware writers or roll back the image without a migration.

## Remaining limits

### Worker lifetime and rebuild diagnosis

The deployment defaults `workerMinReplicas` to **1**, with maximum replicas 1.
This intentionally keeps the 2-vCPU/4-GiB worker allocated during queue lulls;
it incurs continuous cost. Setting the parameter to 0 explicitly opts back into
scale-to-zero and recurrent cold-build latency. The 300-second cooldown is not
a substitute for the replica floor. Restarts for deployments, platform events
and OOMs remain possible. No live resources are updated by changing the template.

Use Container Apps system logs to establish why a replica terminated: SIGTERM
alone does not prove autoscaler scale-down. Cold-build logs now contain `pid`,
`build_attempt`, and a start log's `first_build_in_process` flag. Cache invalidation
logs separate `ownership_changed` and `source_revision_changed`. Correlate these
with replica/revision identifiers: PIDs can repeat in separate containers.
Repeated builds in one process may reflect source revisions, library switching,
adapter replacement, or recovery from write failures; keeping a replica warm
does not fix those causes. Matching checkpoints provide restart recovery;
unsnapshotted assignments still require cold rebuilding as described above.

- ANN retrieval can miss a competitor; exact reranking cannot restore a
  candidate that was not retrieved. Overfetch is capped at 1024 per source.
   Reaching that cap with only one distinct identity suppresses automatic
   merging instead of treating the missing runner-up as a zero score.
- One active library is cached per process. Switching libraries, restart or
   ownership transfer attempts durable restore; stale/missing checkpoints and
   external curation require a cold build. No separate index-build job exists yet.
- Cold build and delta compaction run in the assignment worker under the lease;
  training pauses that library's assignment. Uploads remain independent.
- Local exact storage needs approximately 2 GiB for 1M 512-dimensional vectors,
  plus SQLite/index/spool overhead. Memory accounting is a conservative
  admission guard, not an RSS guarantee. Provision sufficient ephemeral disk.
- Blob leases coordinate assignment workers, not all HTTP curation. They cannot
  fence a Table write already in flight after lease loss. ETags mitigate races;
  a transactional work journal and stronger cross-table recovery remain work.
- Legacy person `faceIds` is still maintained for API compatibility and limited
  to 60 KiB UTF-16. Oversized identities fail explicitly instead of truncating.
  The membership table is not yet the sole authoritative source.
- This is a live assignment migration, not a destructive reset of existing
  people. No million-face throughput or deployment capacity is certified by
  unit tests. Existing deployment CPU/RAM settings are unchanged.