# Vision optimization priorities after qualification batching

Evidence: clean wave `dc2d2f130d174596a66d128810b6df4e`, 666 processed
images, four attempts, no source failures. Build `704f626`, concurrency 3.
Productive Vision elapsed 358.65 seconds, approximately 6,685 images/hour.
Log Analytics workspace `6ca9803c-dd23-4393-a80a-c1fe035e7df9`,
2026-10-29 17:16–17:23 UTC. Raw extraction is held locally at
`/tmp/photostore-gains-clean.json` (666 image summaries, 7,992 apply phases,
four attempt summaries). No production mutations were made for this analysis.

## Where wall time goes

Attempt wall totals 358.16 seconds: source pool 290.74 seconds (81.2%),
post-source settlement 49.94 seconds (13.9%), remaining 17.49 seconds (4.9%).
Admission 46.06 seconds occurs inside the pool and overlaps active workers.
It must not be added as a separate serial cost.

Source slot occupancy is 88.9%: 775.22 busy-worker seconds out of 872.21
capacity-worker seconds. Approximately 32.33 wall-equivalent seconds are unused
capacity, including page tails; this is not a demonstrated removable delay.
Main process CPU totals 465.91 seconds, approximately 700 CPU-ms/image and
1.30 average cores. Resident OCR threads are included.

## Largest source costs

Per-image elapsed values overlap across the three source workers. These are
candidate sizes, not additive wall-time savings.

| Component | Mean ms/image | Interpretation |
| --- | ---: | --- |
| Face embedding | 219.1 | Largest individual model execution interval |
| Face detection | 99.2 | Face pipeline also includes 20.0ms landmarks |
| CLIP image inference | 144.6 | Image/text use the shared model lock |
| CLIP text inference | 107.0 | Inside semantic refresh/apply |
| Face persistence | 149.5 | 12.84 storage calls/image; 138.2ms SDK elapsed |
| Artifact capture | 73.5 | 5.95 calls/image |
| Completion delivery | 58.5 | 5.08 calls/image |
| Derivative promotion | 50.3 | Five calls/image |
| OCR recognition | 29.2 | p95 118.1ms; lower aggregate priority |

All source steps total 568.2ms/image; apply totals 534.6ms/image. Semantic
refresh (126.7ms) belongs to apply, not the source-step total. CPU and SDK
elapsed are not exclusive partitions of these intervals.

## Qualification finding

Post-source prefetch took 32.29 seconds. Existing manifest/face/proof phase
counters span both selection and post-source work: face receipt fetch totals
22.92 seconds, manifests 7.06 seconds, proof/inbox fetch 2.31 seconds.
The first 166-image attempt accounts for 13.96 seconds of face receipt fetch;
later attempts average approximately 18ms/image for that phase. The discrepancy
is localized, but its underlying service/payload cause is not yet established.
Receipt-cache disk reads/writes are zero. Batch queue time totals only 0.95
seconds, with no throttling or server errors. Disk spill or worker-pool queueing
therefore does not explain most of the opening delay.

## Recommended experiments

1. Matched CPU-runtime benchmarks for AdaFace and CLIP image/text execution.
   Keep model weights and preprocessing unchanged. Compare runtime graph
   optimization, CPU thread allocation and, where feasible, equivalent CLIP
   ONNX execution. Require embedding/retrieval/face identity quality checks.
   Existing AdaFace export has fixed batch size one; batching needs a separately
   validated export. Do not simply raise source concurrency.
2. Matched metadata/manifest/face-receipt query versus bounded point reads on
   representative populated artifact partitions. Measure bytes, response
   service time and local decoding separately. Preserve fresh source checks.
   Current production retains a fresh source-state read in addition to the
   final qualification metadata read; its path differs from the prototype.
3. Reuse committed immutable receipts within capture/delivery, then consider
   request-scoped reuse of proven face snapshots after successful persistence.
   Retain source, generation, curation and publication fences. Prior Azure
   receipt-reuse experiments showed component gains; cross-image face batching
   alone and compute/apply handoff did not show compelling gains.

To reach 10k/hour, 666 images must finish in 239.76 seconds: 118.89 seconds
less than this run, a 33.1% wall reduction. Eliminating all settlement would
still leave approximately 309 seconds. A substantial source-pool improvement
is required. A 30% reduction in the measured face-embedding plus CLIP image/text
intervals corresponds to about 31 wall-equivalent seconds at three occupied
slots; that is an illustrative size estimate, not a forecast. Larger gains may
require more CPU capacity or acceleration, assessed separately from algorithmic
efficiency and cost/image.

For clustering, the completed 1,000-member wave took 142.21 seconds: assignment
persistence 49.41 seconds, snapshot preparation 41.67 seconds and journal write
37.26 seconds versus matching 2.01 seconds. Phase counters are inclusive; do not
sum them as disjoint costs. Storage paths have much larger opportunities than
matching algorithm tuning for that run.
