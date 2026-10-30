# Local follow-up to the 10k-gap recommended experiments — 2026-10-29

Scope: local-hardware runs for recommendation 1 (matched AdaFace/CLIP CPU
runtime) and recommendation 3 (receipt reuse / cross-image face batching) from
[vision-next-gains-2026-10-29.md](../audits/vision-next-gains-2026-10-29.md).
Recommendation 2 (manifest/face-receipt query vs bounded point reads) is **not
covered here**: the only existing runners for that comparison
(`benchmark_vision_reads.py`, `benchmark_vision_wave_scan.py`) query the live
`forenklaprep1devmqwhngus` production storage account with Azure CLI
credentials: that is a production/cloud action, not a local benchmark, and was
out of scope for this pass.

All runs used this Mac (macOS 26.6.2 arm64, Apple M4 Pro, no CPU-quota
cgroup), the repository's `.venv-photostore-tools` interpreter, and real
production code paths. No production data, credentials or deployments were
touched. Raw JSON is linked per section.

## 1. AdaFace/CLIP matched CPU runtime (`backend/scripts/benchmark_vision_stages.py`)

Same 48-image local corpus and runner used in the prior
[vision-stages-local-20261027.md](vision-stages-local-20261027.md) benchmark,
rerun today at the current build with explicit native-thread pinning
(`OMP_NUM_THREADS`/`OPENBLAS_NUM_THREADS`/`MKL_NUM_THREADS`/`VECLIB_MAXIMUM_THREADS`)
to isolate the CPU-thread-allocation question raised by the recommendation.
Real YOLO/MediaPipe/AdaFace/CLIP ViT-B-32-quickgelu/Tesseract ran through the
current processor functions; models were warmed first. Two rounds per
configuration; all result hashes matched across rounds.

| Config | Workers | Native threads | 48-img wall | img/s | Face detect mean | Face embed mean | CLIP inference mean |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 1 | 1 | 5.90 s | 8.14 | 37.83 ms | 10.12 ms | 16.54 ms |
| B | 2 | 2 | 3.29 s | 14.61 | 38.78 ms | 10.19 ms | 22.75 ms |

Raw: [vision-stages-local-20261029-1thread.json](vision-stages-local-20261029-1thread.json),
[vision-stages-local-20261029-2thread.json](vision-stages-local-20261029-2thread.json).

Observations, held to the same caveats as the 2026-10-27 local report — this
is not the deployed two-vCPU worker, and MediaPipe here uses native macOS
graphics acceleration rather than a Linux CPU-only path:

- Per-image face detection (37.8 ms) and face embedding (10.1 ms) are far
  below the Azure-deployed means in the qualification run (99.2 ms and
  219.1 ms respectively). Even allowing a large discount for hardware,
  quota and acceleration differences, the gap is wide enough that the
  recommended matched comparison on the actual deployed CPU target (not this
  Mac) is still the right next step before concluding how much is headroom
  versus how much is irreducible on that hardware.
- CLIP inference mean (16.5–22.8 ms here) is similarly far below the
  deployed 144.6 ms CLIP image mean. The same caveat applies.
- Doubling native thread count alongside worker count (B vs A) raised batch
  throughput 1.80x (8.14 → 14.61 img/s) but did not change per-image face
  stage cost, and CLIP inference per-call cost rose (16.5 → 22.8 ms mean) —
  consistent with threads contending rather than each image getting cheaper.
  This says threading helps with concurrency, not with the per-image runtime
  reduction the recommendation is primarily after.
- This pass did not add an ONNX CLIP comparison path (the recommendation
  listed it as "where feasible"); `benchmark_vision_stages.py` has no such
  mode today. Building one would need a separately exported/validated ONNX
  CLIP graph, which the original recommendation also flagged as unresolved
  for AdaFace batching.

This local run does not establish deployed-worker headroom. It narrows what
"matched CPU runtime" should mean: compare against the actual Linux
container CPU quota, not this machine, before sizing any model-level change.

## 2. Receipt reuse and cross-image face-write batching (`backend/scripts/benchmark_vision_persistence.py`)

Recommendation 3 asked for immutable-receipt reuse in capture/delivery and,
separately, cross-image face-write batching. Both prototypes already exist in
this script; they previously ran only against real Azure (private temporary
storage account, [vision-persistence-azure-20261029.md](vision-persistence-azure-20261029.md)).
This run points the same script at a local Azurite table emulator
(`mcr.microsoft.com/azure-storage/azurite`, Docker, table port 10002 only) so
the comparison is fully local — no cloud resources, credentials or production
data. 32 synthetic images per configuration, two rounds, alternating mode
order; every run's output digest matched across modes and rounds (face
persistence, embeddings, lookup state and completion delivery contents were
byte-identical regardless of which prototype produced them).

Median wall time / request count per image, 1-face-per-image case, 8-image
flush groups:

| Mode | ms/image | Requests (32 images) |
| --- | ---: | ---: |
| current | 22.3 | 832 |
| batch (cross-image face write) | 25.3 | 776 |
| receipts (reuse committed receipts) | 20.4 | 768 |
| combined | 25.0 | 712 |

Guard-parallel variants (`batch_parallel`/`combined_parallel`, 8/16-image
groups): [vision-persistence-local-20261029-parallel.json](vision-persistence-local-20261029-parallel.json)
— `receipts` and `combined_parallel` were still the two fastest modes
(20.1–21.1 ms/image vs 22.1–23.1 ms/image for `current`).

Full data: [vision-persistence-local-20261029.json](vision-persistence-local-20261029.json).

Reading this against localhost Azurite (sub-millisecond RTT) rather than
Azure:

- **Receipt reuse** is the one change that was faster in every configuration
  measured, including against a near-zero-latency backend: it simply removes
  requests (768 vs 832, -7.7%) with no compensating overhead. This is the
  same direction as the real-Azure result and strengthens the case that it's
  a structural win, not just a network-latency win.
- **Cross-image face-write batching** (`batch`) was *slower* locally in most
  configurations (25.3 ms/image vs 22.3 ms/image at 1 face/image) despite
  fewer requests (776 vs 832). Against Azurite there's no RTT to hide, so the
  batching coordination overhead (guard reads, grouping, transaction paging)
  shows up as a net cost. This matches the audit's own prior finding ("cross-
  image face batching alone... did not show compelling gains") and adds a
  local data point for *why*: the benefit is specifically network-latency
  hiding, which this environment can't reproduce — consistent with needing
  the real Azure SDK/transport to judge it, as the recommendation says.
- `combined` tracks `batch`'s overhead at low image volume but the
  parallel-guard follow-up narrows the gap at 8/16-image groups, suggesting
  guard parallelism is necessary (not optional) if batching is pursued
  further.

Net: this local pass supports prioritizing receipt reuse first and treating
cross-image batching as still unproven, matching the recommendation's own
ordering. It does not replace the required next step — running the same
variants through the real Azure Blob/Table SDK against a disposable account —
since Azurite's near-zero latency cannot stand in for Azure's actual service
time, retries or throttling behavior.

## Not run

- Recommendation 2 (manifest/face-receipt/metadata query shape): needs live
  production data and credentials; not attempted here.
- `benchmark_vision_concurrency.py` and `benchmark_vision_pipeline.py`:
  both hardcode `/app/models/face_landmarker.task`, the deployed container's
  model mount path, and are written specifically for the Azure Container Apps
  job harness (cgroup quota reads, job-scoped storage secrets). They were not
  adapted for a local run; doing so would mean testing a different code path
  than what actually executes in production.

Docker/Azurite container used for section 2 was removed after the run
(`docker rm -f photostore-bench-azurite`, verified absent). No production
systems, credentials, or deployments were touched by this pass.
