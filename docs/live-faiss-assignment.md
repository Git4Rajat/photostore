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

Existing assignment threshold/margin settings still apply. The backend/worker
Docker image installs the separate pinned FAISS requirements. The lease and
revision blobs reuse the existing managed-identity Blob client and container;
no resources or credentials are provisioned by the runtime.

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