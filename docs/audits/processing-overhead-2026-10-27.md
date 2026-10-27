# Processing overhead: 851f7c2 fresh upload

Read-only production log inspection, October 27 2026. Wave
`82a439bce7d145ed977579f791c7ca12`, 1,495 images and 1,487 clustering inputs.
Vision and cluster revision 0000017. Source: ContainerAppConsoleLogs_CL,
08:20–09:06 UTC; embedded application timestamps used for elapsed gaps.

## Vision

First work 08:21:30.559858 UTC; frozen 08:58:50.488606 UTC. Wall
2,239.929 seconds; summed queue scope elapsed 1,836.319 seconds.
All 1,495 members settled, zero failed images; one queue-message error.

At 08:47:08.961, commit_observations exhausted the 16 conditional-transaction
retries in WaveStore._retry, raising Concurrent wave mutation. Next scope began
08:52:10.351: a 301.4-second gap. Logs identify the failing operation but do not
identify which transaction entity or competing writer caused the conflict.
Other continuations have roughly 6.1-second gaps. The code checkpoints around
100 seconds and releases healthy continuations with retry_delay=5.

Per-image phase elapsed totals (sum of 1,495 calls each):

| Phase | Total seconds | Mean milliseconds/image |
|---|---:|---:|
| Derivative promotion | 360.887 | 241.40 |
| Face persistence | 310.066 | 207.40 |
| Artifact capture | 290.192 | 194.11 |
| Semantic refresh | 150.449 | 100.63 |
| Embedding write | 23.052 | 15.42 |
| Metadata CAS | 23.038 | 15.41 |
| Lease and policy check | 14.546 | 9.73 |
| Index dirty | 13.453 | 9.00 |

These are elapsed phase totals across concurrent sources, not additive wall
segments. Shared-scope SDK deltas also include concurrent work and cannot be
attributed exactly to an individual phase.

Scope top-I/O summaries consistently include 37,936 job-table GETs (548.011
aggregate SDK seconds), 33,032 metadata GETs (495.772 seconds), 10,673 face
filename-index GETs (90.479 seconds), 7,475 thumbnail-container GETs (83.914
seconds) and 8,970 thumbnail HEADs (64.002 seconds). SDK durations overlap.

Recommendations:
1. Handle transient coordination conflicts with bounded short retry/reschedule
   after releasing ownership, preserving durable source results and admission
   counters. Record failed transaction operation/index/status and retry duration
   to identify the contended row rather than assuming a particular competitor.
2. Remove the fixed delay for productive continuation, or safely extend its
   lease-renewed processing budget. Preserve fairness and shutdown handling.
3. Reuse exact ETag-qualified prepared bytes between preview use and derivative
   promotion; consolidate artifact proof capture. Publication already caches
   verified derivative receipts inside the active result fence: extend that
   existing path rather than add a second generic cache. Preserve fresh claim,
   final result CAS, source replacement and artifact ETag checks.
4. Batch face/index reads and writes around a source/group, avoiding repeated
   discovery; source metadata currently gets read about 22 times/image. Do not
   blindly cache ResultFence.check, which protects actual artifact writes.

## Clustering

Measured assignment elapsed 325.023 seconds, CPU 62.158 seconds, SDK calls
20,234 and overlapping aggregate SDK send time 253.894 seconds.

| Phase | Elapsed seconds | SDK calls |
|---|---:|---:|
| Snapshot preparation | 152.564 | 9,585 |
| Assignment persistence | 104.694 | 8,684 |
| Journal writes | 45.770 | 162 |
| Eligibility checks | 18.631 | 1,878 |
| Curation checks | 16.178 | 1,595 |
| Matching | 5.428 | 0 |
| Runtime update | 2.177 | 0 |
| Runtime build | 0.814 | 6 |
| Publication handoff | 0.313 | 32 |

Inclusive phases overlap: journal writes and guard checks occur within larger
phases. Do not sum this table. Matching is 1.7% of total assignment elapsed;
accelerating matching alone offers little improvement.

Top-level queue scope: people GETs 5,290/69.800 seconds (2,334 error responses,
which include expected missing-row probes), face GETs 2,778/26.317 seconds,
embedding GETs 1,151/22.978 seconds, jobs GETs 6,495/80.730 seconds.

Recommendations:
1. Batch immutable input manifest/face receipt loading by bounded wave pages.
   Snapshot preparation currently resolves receipt references through individual
   reads. Load baseline people in a bounded stream/query and reuse a snapshot
   map, including missing deterministic IDs during this capture. Existing cold
   face/embedding streaming is already enabled; do not propose enabling it as a
   new optimization.
2. Combine curation witness and materialization face reads, retaining the same
   genuine ETag for the subsequent conditional face write. Consolidate person
   updates per target; batch same-table/same-partition effects where possible.
   Preserve final face stamps and membership prerequisites. Current membership
   partitions differ by person, so a universal single transaction is infeasible.
3. Group mounted journal flushes at bounded decision/application boundaries.
   Current FileAssignmentJournal.put flushes and fsyncs every non-preparation
   record; 10,294 journal calls took 45.770 seconds. Some cost is preparation
   indexing/serialization and guards, so this is not a measured fsync saving.
   A decision group must be durable before its materialization; completion must
   follow all required effects. Instrument flush count/time separately.
4. Keep eligibility checks at real write boundaries while avoiding redundant
   refresh sequences. Guard_check elapsed is 18.791 seconds and overlaps the
   phases above, a smaller target than snapshot/persistence.

No deployment, queue, configuration or application-code mutation was performed.

## Implemented locally after the inspection

- Distinct MutationContention preserves Conflict compatibility and logs exhausted
  local retry count, operation, elapsed time and SDK transaction index. Vision
  releases the dispatcher and sends a normal wake after 2/4/8/16/30 seconds.
  Each conflict remains charged to the existing dispatch attempt limit. Other
  validation/ownership errors and non-Vision stages retain their failure path.
  Queue-send failures propagate and leave the durable dispatch reservation.
- Healthy, progressing Vision continuations use zero requested delay; idle or
  failing passes keep their delay. A replay integration test fails observation
  commit after a completed source and proves the next normal wake settles its
  existing receipt without another model attempt.
- Clustering batches completion inboxes, immutable manifests and face snapshots
  with bounded OR-key queries. Every digest, reference and source identity is
  still verified. Cache size is bounded at 4,096 immutable rows. Twenty complete
  single-face inputs need eight queries including member paging and zero point
  reads. Snapshot people reads are also batched; existing cold embedding
  streaming stays enabled.
- Clustering persistence shares the curation face read and its genuine ETag
  with the conditional materialization write. New membership uses create-only;
  an existing row must still be read and validated. Person curation, lost replies,
  partial publication and mounted journal recovery regression tests pass.
- Normal derivative reads use a capped GET with response properties instead of
  HEAD+GET. Receipt verification conditions the GET on its stored ETag. Blob
  read-back, byte/hash/size/dimension checks and create-only publication remain.
  Two prepared derivatives take four blob GETs, zero HEADs and two fresh source
  checks rather than seven. Artifact capture removes one more redundant source
  read, keeping its entry and final checks. New immutable face/manifest receipts
  use create-only without first reading; conflict/retry still proves equality.

Read-count limits: this removes six known metadata reads for a source that
promotes both derivatives and captures a receipt. It does not establish the new
full-image average or reduce total reads from 22 to one. Claims, mutable artifact
writes and final metadata commits still need fresh ownership/generation evidence.
A single total read would require a different publication/fencing design.

The publication change can leave an unreferenced immutable blob if source
ownership changes during preparation. The post-read-back source fence rejects
its pointer, and the final metadata CAS remains fresh. No mutable artifact write
uses a cached ResultFence approval. This is the same orphan-safety model already
used when a source changes during upload; the source-change regression test now
checks successor preservation and absence of a published pointer.

Validation: 1,159 regression tests passed across wave, processing, FAISS,
clustering, index-work and filename lookup suites. After the final receipt
create-only change, another 93 targeted receipt, source-batch, prepared-artifact,
execution-consumer and runtime tests passed. git diff --check passed.

The 1,000-face synthetic comparison against 851f7c2 preserves assignment output:
8,022 -> 5,093 SDK-method calls (36.5% fewer). Face GETs: 2,000 -> 1,000;
membership GETs: 1,000 -> 0; people snapshot point reads: 1,002 -> 73 queries.
It excludes the additional cloud receipt-loading savings and simulates no Azure
network latency, so its local elapsed times are not production predictions.
See ../benchmarks/processing-overhead-20261027.json. No cloud mutations or new
production measurements were performed during this implementation.
