# Vision concurrency 2 versus 3 on Azure

Three workers help when source workers spend enough time waiting, but regress
compute-heavy processing. Production configuration was not changed.

## Controlled setup

- Current worker image `32b9e9b7e014e1eeb8204ceff6536a0ffad94006`, isolated manual
  Container Apps job in West Europe: 2 CPU / 4Gi. Observed cgroup quota
  `200000 100000`. Five critical worker module hashes match local source.
- No managed identity, production credentials, production mounts, or production
  data access. Face persistence used four tables in a private temporary account.
- Same real CLIP weights, face detector/landmarks/AdaFace, and reusable OCR engines.
  Shared decode enabled; native single-thread limits retained.
- 48 mixed inputs (45 distinct byte sequences), plus a 15-input face-positive
  subset selected by production face-quality rules. Each sequence repeated twice.
  Four rounds with 2/3 ordering reversed on alternate rounds: 48 comparisons,
  3,024 measured image passes. Warmup and input/metadata seeding excluded.
- The source executor varies between two and three workers. Both variants share
  the warmed process, models, and OCR pool (job `IPWORKER_CONCURRENCY=3`); this is
  a component comparison, not the complete application queue consumer.

## Median results

Change is throughput at three workers relative to two. Rates are experimental
component/replay capacities, not production full-upload throughput.

| Profile | Scenario | Two workers | Three workers | Throughput change |
| --- | --- | ---: | ---: | ---: |
| Mixed, 96 images | OCR + face + image CLIP | 20.324s | 24.927s | -18.5% |
| Mixed, 96 images | Above + real semantic encoding + Azure face persistence | 28.614s | 30.189s | -5.2% |
| Mixed, 96 images | Above + simulated residual apply wait | 39.499s | 31.744s | +24.4% |
| Face-positive, 30 images | OCR + face + image CLIP | 8.125s | 10.336s | -21.4% |
| Face-positive, 30 images | Above + real semantic encoding + Azure face persistence | 11.305s | 12.245s | -7.7% |
| Face-positive, 30 images | Above + simulated residual apply wait | 15.093s | 13.109s | +15.1% |

The residual replay adds 260ms sleep after real face persistence. This approximates
the production apply duration remaining after semantic encoding and face storage;
it does not recreate actual derivative/receipt/completion writes, their CPU work,
coordination, claims, shutdown, or final qualification. Treat the gain as a
sensitivity result. In particular, the mixed replay's approximately 10,887/hour
is not evidence that the production app reaches 10k/hour.

Compute-only runs consume about 1.95–1.99 process CPU cores. Three workers add
contention on the same CPU quota. In the mixed residual replay, process CPU use
rises from approximately 1.18 to 1.62 cores; in the face-positive replay, from
1.23 to 1.67 cores. This supports the mechanism of hiding waits. Process CPU
covers the resident OCR worker threads; cgroup deltas in the raw report include
all container CPU consumption and throttling. Reported peak RSS is process-lifetime
high-water memory, not an isolated per-variant memory comparison; maximum 2.41GiB.

## Correctness and limitations

All per-input model and semantic output hashes match across configurations.
All per-profile/scenario aggregate hashes match over all eight runs. Durable
face lookup completeness, face count, and source revision bindings were checked
after each storage run. No face transaction errors, embedding-version read errors,
OCR timeouts, or OCR capacity errors were recorded.

Persistence invokes the real `_store_client_face_entities` implementation with
fresh source-fenced metadata and prepared complete-empty filename lookups. Crop
warming and unrelated search-index invalidation are disabled. It does not validate
all application commit semantics or reproduce the size/query latency of the live
library partition. No thresholds, models, precision, or source fences changed.

The corpus is small and the face-positive subset is more relevant to the recent
face-heavy upload than the mixed corpus, but neither is a full 9k-image replay.
Repeated inputs limit corpus diversity. No precision/quality conclusion beyond
the matched outputs on these inputs is implied.

## Decision

A controlled fresh-upload trial at concurrency 3 is justified while production
still spends substantial time in apply/storage. It is not justified as a permanent
default from this component test alone. Compare useful terminal images/hour,
container CPU/throttling, p95 source/apply latency, memory and failures using
identical image selections. As persistence gets faster, retest: the optimal
concurrency can return to two under the same CPU quota.

The supported sensitivity gain is approximately 15–24%, not the roughly 75%
needed to move the observed 5,700/hour peak to 10k/hour. This tuning may help,
but does not replace persistence and scheduling work or additional CPU capacity.

Raw measurements: [vision-concurrency-azure-20261029.json](vision-concurrency-azure-20261029.json).
Runner: `backend/scripts/benchmark_vision_concurrency.py`. The controller's private
credentials and input bundle are not retained in the repository.

The temporary job and storage account were deleted, and absence verified at
2026-10-29 11:38:45 UTC. Production configuration remained unchanged.
