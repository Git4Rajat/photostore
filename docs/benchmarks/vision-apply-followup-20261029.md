# Vision persistence/apply follow-up — 2026-10-29

## Live baseline

Wave `5e19459990b84d3bbbfff13322defdf2` used image `b7a5222`, including the
committed immutable receipt cache. All 1,001 members settled with no failures;
there were 1,000 fresh source attempts.

Peak throughput was **126 unique fresh source completions in a rolling 60-second
window, or 7,560 images/hour**, from 21:25:31.361419 to 21:26:31.361419 UTC.
Count the first successful processing summary per `file_id`, excluding replayed
inspection-only summaries. This is a one-minute observed peak, not a sustainable
full-upload rate. Active attempt time was 580.73s; Vision start-to-freeze elapsed
was 858.55s because of an artifact cleanup/retry interruption.

| Apply phase | Mean ms/image | p95 ms/image | SDK calls/image |
| --- | ---: | ---: | ---: |
| Face persistence | 183.46 | 231.47 | 12.81 |
| Semantic refresh (includes text inference) | 142.88 | 228.52 | 0 |
| Artifact capture | 88.53 | 144.04 | 5.94 |
| Derivative promotion | 56.19 | 79.95 | 5.00 |
| Completion delivery | 44.74 | 73.95 | 3.13 |
| Embedding write | 14.60 | 24.96 | 1.00 |
| Metadata CAS | 13.88 | 21.45 | 1.00 |

Phase times overlap across source workers; totals are not additive wall time.
Some source validation is nested within another phase. The live SDK counters
measure instrumented storage sends; benchmark `requests` below counts outer SDK
method invocations and can differ for transactions, continuation pages and retries.

## Isolated benchmark

`backend/scripts/benchmark_vision_apply_followup.py` compares four modes:

- **production**: existing face writer and deployed `committed_receipt_scope`.
- **capture_parallel**: fetch fresh face and embedding rows concurrently through
  a shared four-thread pool for one-to-three-face sources. Consume each row only
  once, retain all capture validation and the final fresh lookup reread. Four or
  more faces retain the existing consolidated read path.
- **batch_parallel**: previous bounded cross-image write prototype, with parallel
  source/writer checks and lookup publication, plus the deployed receipt cache.
- **batch_capture_parallel**: combine the two experiments.

Five scenarios: zero, one, two and eight faces, plus curated one-face reprocessing.
Four alternating-order rounds, 32 images per comparison, three source threads;
2 CPU / 4 GiB disposable Azure job and real SDK access to five isolated temporary
tables. Each comparison has a new partition. Setup and independent durable
readback are excluded from timing. Source/lookup fences, receipt scope/digests,
embedding identity checks and curated fields remain validated.

This benchmark covers face persistence, capture, metadata CAS and completion
acceptance. It excludes models, semantic refresh, derivative blob promotion,
index-dirty work, actual source coordination and full-upload throughput. The
harness processes persistence and capture in group phases; its speedup must not
be presented as full Vision speedup.

The eight-face cross-image prototype can lose the production writer's consolidated
version reads; include it as a regression control, not an intended production
replacement. Zero-face and eight-face capture-only runs are unchanged controls.

27 local correctness tests passed, including durable output parity, curation,
source/lookup races, partial writes, and lookup/embedding capture validation.
Azure results and resource cleanup are recorded below.

## Azure results

80 comparisons / 2,560 source passes completed successfully. Values are median
paired **component wall-time reductions** against production in the same round;
positive means faster. Four rounds are directional evidence, not tight confidence
intervals or full-Vision speedups.

| Scenario | Parallel capture | Face batching | Both |
| --- | ---: | ---: | ---: |
| Fresh one-face images | +1.2% | +0.7% | -1.5% |
| Fresh two-face images | +2.2% | +12.2% | +15.8% |
| Fresh eight-face images | -0.2% (unchanged control) | -18.3% | -12.0% |
| Curated one-face reprocessing | +7.7% | +0.2% | -1.4% |

Fresh one-face production median: 3.058s per 32 images, versus 3.031s with
parallel capture. Capture plus delivery accounted for approximately 1.837s vs
1.800s. The result is small compared with unchanged-control variability:
zero-face capture (which does no parallel work) ranged from 6.9% slower to 9.2%
faster; eight-face capture (also unchanged) ranged from 2.6% slower to 6.8%
faster. Do not describe these control fluctuations as optimization gains.

Two-face batching improved all four rounds: 7.8–15.2% reduction alone and
13.6–23.6% combined. Production median was 3.984s per 32 images; combined was
3.324s. SDK calls/image were 26 vs 25.25. This saves roughly 21ms of component
wall time per image in this three-source, grouped harness; it is not a prediction
of 21ms off full Vision latency. Fresh one-face SDK calls fell 24 to 22.25 under
batching without a dependable wall gain. Eight-face calls increased 24 to 29.25,
explaining why preserving existing multi-face consolidation is essential.

Curated capture improvements ranged from **3.2% slower to 15.5% faster**. Curated
batching ranged from 35.5% slower to 6.8% faster. This needs more independent runs
before considering production behavior changes.

Independent SDK readback verified **2,560 durable source completions, 6,144 face
snapshots and 11,264 receipt/inbox rows**, including manifest scope/digest and
completion payload parity. Each individual comparison also verified face,
embedding, lookup, receipt and curated-field output parity outside its cache.

## Decision

Do not ship blanket cross-image batching or parallel capture from this evidence.
Most inputs in the latest upload used the single-face/no-face writer: only
10/1,000 fresh source summaries recorded multi-face batched writes (eight with
four rows, two with six). This is an instrumentation observation, not a complete
face-count distribution. The useful two-face result would affect a small portion
of this run and is not a path to 10k images/hour on its own.

Keep both prototypes confined to the benchmark. If revisiting persistence, the
next experiment should eliminate redundant artifact work under the existing
source/writer guards rather than only reschedule the same reads/writes. It must
preserve fresh curation, cleanup policy, lookup generation and cross-table
failure handling. Semantic refresh and derivative promotion are separate costs
not measured by these prototypes.

Temporary benchmark job and storage account were deleted; absence verified at 2026-10-29T22:26:15.652327+00:00. No production code, configuration or roles changed.
