# Live FAISS assignment

Incremental face assignment now defaults to `PEOPLE_ASSIGNMENT_ENGINE=faiss`.
The existing `legacy` engine is an explicit rollback option; FAISS errors do
not silently fall back to library-wide linear scans.

## Data path

1. Acquire and renew one Blob lease per library in the existing people index
   container. Busy/lost leases leave queue work retryable.
2. Cold-build once from ordered, paginated face and embedding Table scans.
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

Existing assignment threshold/margin settings still apply. The backend/worker
Docker image installs the separate pinned FAISS requirements. The lease and
revision blobs reuse the existing managed-identity Blob client and container;
no resources or credentials are provisioned by the runtime.

External person/face mutations publish source revisions; ownership transfer or
revision changes invalidate the local snapshot. New unowned upload faces do
not invalidate the assigned-face index. Assignment writes bypass those hooks
and update the bounded delta directly.

## Remaining limits

- ANN retrieval can miss a competitor; exact reranking cannot restore a
  candidate that was not retrieved. Overfetch is capped at 1024 per source.
   Reaching that cap with only one distinct identity suppresses automatic
   merging instead of treating the missing runner-up as a zero score.
- One active library is cached per process. Switching libraries, restart,
  ownership transfer or external curation requires a cold build. There is no
  durable cloud FAISS checkpoint or separate index-build job yet.
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