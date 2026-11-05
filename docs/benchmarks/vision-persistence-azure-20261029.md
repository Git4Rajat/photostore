# Vision face persistence and receipt reuse benchmarks

Receipt reuse is the useful next implementation. Cross-image batching alone did
not show a reliable elapsed-time improvement, despite fewer SDK calls. Do not
replace the existing multi-face batch path with this cross-image prototype.

## Setup and measured scope

128 comparisons, four alternating-order rounds per scenario, 32 images per
comparison: **4,096 measured image passes**. Real Azure SDK IO in a disposable
West Europe Container Apps job, 2 CPU / 4Gi, three source threads. Image:
`photostore-ipworker:32b9e9b7e014e1eeb8204ceff6536a0ffad94006`.
The production face, receipt, fence and completion modules match this revision.

Five tables in a new private temporary storage account held synthetic source
metadata, deterministic 512-dimensional embeddings, faces, filename lookups and
receipts/inboxes. Each comparison used a fresh library partition. Curated
controls first seeded faces and marked them confirmed with a person assignment.
Setup, seeding and output readback are excluded from the timings.

The timed sequence is production face persistence, artifact snapshot/manifest
capture, source metadata CAS and `ShadowWaves.accept_source_completion`. The
other image steps are represented by terminal no-data statuses. Models, semantic
refresh, blob promotion, actual wave coordination, completion ACK, queue handling
and asynchronous crop/search side effects are excluded. Thus these numbers are
**storage-component wall times, not full-image or full-upload throughput gains**.
There is no simulated network sleep.

The job had no managed identity, production storage credentials or production
mounts. No production code, defaults, application configuration or roles changed.

## Prototypes

1. **Cross-image face writes:** queue bounded groups, preserve original artifact
   ETags, write embeddings before faces, and publish complete filename lookups
   only after both tables succeed. Fresh source/filename-writer guards remain.
   The initial version serialized guards/publication at flush; the follow-up
   executes those independent checks and publications with at most three threads.
2. **Receipt reuse:** cache only successfully committed immutable manifest/face
   receipt rows within the same capture-and-delivery call. Completion verification
   still validates scope, payload hashes and snapshot coverage. The cache expires
   on exit. Source metadata, filename lookups, cleanup policy and missing rows are
   not cached. Mutable face/embedding reads during capture remain unchanged.

Both changes were also measured together. A face batch is not considered a
completed source until its flush and lookup publication succeed.

## Results

Percentages below are median **paired wall-time reductions**, matching each
candidate with the current path in the same scenario and round. Positive means
less elapsed time; negative means slower. Four rounds establish a useful
direction, not a tight confidence interval.

| Initial matrix | Naive face batch | Receipt reuse | Both |
| --- | ---: | ---: | ---: |
| One face, group 1 | -4.9% | +2.7% | +1.3% |
| One face, group 8 | -34.2% | +2.4% | -31.6% |
| One face, group 16 | -35.4% | +5.8% | -20.7% |
| Zero faces, group 8 | -31.7% | +3.1% | -26.7% |
| Eight faces, group 8 | -36.1% | +16.6% | -17.4% |
| Curated one-face reprocessing, group 8 | -35.9% | +5.4% | -28.1% |

| Follow-up: parallel guards and lookup publication | Face batch | Receipt reuse | Both |
| --- | ---: | ---: | ---: |
| One face, group 8 | +1.2% | +7.6% | +7.6% |
| One face, group 16 | -0.7% | +5.2% | +9.7% |

For the parallel batch alone, per-round changes ranged from **-5.5% to +2.1%**
at group 8, and **-3.2% to +5.3%** at group 16. This is not a convincing gain.
Adding batching to receipt reuse was a median **2.1% slower** at group 8 and
**4.4% faster** at group 16; even that group-16 increment ranged from -2.4% to
+5.2%. The combined group-16 gain versus current ranged from -0.3% to +15.8%.

Receipt reuse has a clearer signal: all single-face paired comparisons improved;
eight-face improvements ranged from **10.5% to 20.4%**. Zero-face results were
smaller and included one negative round. Mutable capture reads remain a cost.

### Calls and where time remains

`requests` in the raw JSON means **outer SDK method invocations**, not HTTP wire
attempts, internal retries or continuation-page counts. A transaction is one
counted call containing multiple operations.

| Fresh scenario | Current calls/image | Face batch | Receipt reuse | Both |
| --- | ---: | ---: | ---: | ---: |
| One face, group 8 | 26 | 24.25 | 24 | 22.25 |
| One face, group 16 | 26 | 24.125 | 24 | 22.125 |
| Zero faces, group 8 | 15 | 15 | 14 | 14 |
| Eight faces, group 8, naive batch | 33 | 38.25 | 24 | 29.25 |

Receipt reuse removes the manifest read plus one read per face snapshot: two
reads for one face, nine for eight faces. It does not remove mutable capture
proof checks or bypass the cleanup policy read.

Batching one-face images cuts 64 individual face/embedding writes per 32 images
to eight transactions at group 8, or four at group 16. Yet fresh guards,
preparation and lookup publication remain. In the parallel group-16 batch,
median wall contributions per 32 images were roughly 285ms for guards, 154ms for
transaction submission, and 221ms for lookup publication, before the remaining
preparation work. Cutting write calls alone leaves much of persistence intact.

For eight-face inputs the prototype also loses today's consolidated embedding
version reads, replacing them with individual reads. That increases SDK calls.
Any later cross-image design must retain the existing multi-face path rather
than apply this prototype indiscriminately.

## Correctness and durability

- Output digests match across all variants/rounds for each scenario. Readback
  checks complete lookups, face counts, bounding boxes, embeddings and curation.
- An independent fresh SDK read verified **4,096 durable source completions,
  7,168 face snapshots and 15,360 receipt/inbox rows**, including manifest scope,
  payload hashes, snapshot hashes and exact completion-inbox payloads. This
  verification ran outside the receipt cache.
- **43 local tests passed**, covering prototype parity plus the existing receipt
  suite. Prototype cases include stale source attempts, lost filename writers,
  concurrent face/embedding edits, partial cross-table failures, partial snapshot
  failures, no caching before successful writes, scope/digest validation, cache
  cleanup, changing lookups and fresh cleanup-policy rejection.
- Azure runs verify successful-path SDK transactions/CAS and durable outputs;
  injected race/failure cases use ETag-aware local table doubles, not a live
  Azure chaos test.

## Decision and limits

Implement request-scoped committed receipt reuse first, preserving hash/scope
verification and all fresh mutable guards. Instrument cache hits/misses, avoided
reads, cached bytes, capture/delivery wall time and fallback/conflict counts.

Defer cross-image production batching. The next face-persistence experiment
should reduce redundant guard/read/publication work while preserving ownership
and curation semantics, rather than only combining writes. If batching is
revisited, retain the multi-face path, flush partial groups promptly and validate
the incremental gain over receipt reuse alone.

New small partitions do not reproduce large production-library query latency.
Synthetic inputs do not cover every face distribution or payload shape. All
model results are immediately available here; real arrival skew and a batch
linger timeout can add latency, especially on small uploads. Three source
threads at group 1 have only one runnable source. No 10k-images/hour claim follows
from this component benchmark, and no full-upload gain has yet been measured.

Raw results and exact runner snapshots:
[vision-persistence-azure-20261029.json](vision-persistence-azure-20261029.json).
Runnable prototype: `backend/scripts/benchmark_vision_persistence.py`;
correctness checks: `backend/tests/test_benchmark_vision_persistence.py`.

Temporary-resource deletion and absence verification are recorded in the raw
report after cleanup.
