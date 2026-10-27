# Vision core batching on Azure — 2026-10-27

The proposed synchronous CLIP broker does **not** improve the current two-source
pipeline. Standalone CLIP benefits from full batches, but almost every pipeline
request flushes as a singleton. Keep `IPWORK_CLIP_IMAGE_BATCH_SIZE=1` (disabled).
No production image, configuration, queue, table or role was changed.

## Full OCR/face/CLIP core comparison

48 inputs, four rounds per configuration, with configuration order reversed on
alternate rounds. Two source workers, shared decoding enabled, identical images,
models, weights and worker dependencies. Timings include broker creation/drain
and source executor work, but exclude storage, preview creation, model startup,
warmup, artifact handling, semantic text embedding and persistence.

| CLIP mode | Window | Wall for 48 inputs | Change vs baseline | Process CPU |
| --- | ---: | ---: | ---: | ---: |
| Existing singleton path | — | 10.451 s | — | 20.250 s |
| Broker, max batch 2 | 0 ms | 10.698 s | 2.4% slower | 20.706 s |
| Broker, max batch 2 | 1 ms | 10.728 s | 2.7% slower | 20.697 s |
| Broker, max batch 2 | 2 ms | 10.766 s | 3.0% slower | 20.784 s |
| Broker, max batch 2 | 5 ms | 10.797 s | 3.3% slower | 20.517 s |

Of 768 requests submitted to the broker, just six images formed three actual
pairs. All 0/1/2 ms requests were singleton flushes; the 5 ms configuration formed
three pairs across its four rounds. Source workers reach CLIP at different times
after OCR and face processing. In this experiment the short window seldom
collects a second tensor, while queue/future handoff and waiting add overhead.
There were no batch fallbacks or model/source failures.

All 960 timed core image passes retained tag order, person decisions, 17 accepted
faces across 15 inputs and OCR text presence on seven inputs. Six full-result
hashes changed because paired CLIP floating-point outputs differ slightly:
maximum absolute embedding difference 0.0000002385; minimum cosine similarity
0.9999999999998974. The hashes are not claimed identical.

The baseline used about 1.94 CPU cores on average (20.250 CPU-seconds / 10.451
wall-seconds), close to the two-CPU quota. This core-only test has little idle
CPU capacity; it does not measure production storage/orchestration gaps.

## Standalone model results

The CLIP experiment has full serial batches available immediately. It includes
JPEG decode, existing preprocessing, inference/normalization and production tag
scoring. It excludes queue formation and competing OCR/face work. Four rounds
per configuration, reversed order on alternate rounds:

| CLIP batch | Amortized wall ms/image | Forward + normalization ms/image |
| --- | ---: | ---: |
| 1 | 124.78 | 103.03 |
| 2 | 104.02 | 82.08 |
| 4 | 92.12 | 70.78 |
| 8 | 88.73 | 67.90 |

Batch 2 saves 16.6% of this standalone CLIP cost; batch 8 saves 28.9%.
All 768 timed passes retained tag/person decisions. Maximum absolute embedding
difference is 0.0000002981. This is output consistency on a small corpus, not an
accuracy evaluation. Amortized cost is not individual image latency; the report
records batch service latency separately, without queue formation delay.

YOLO batching was slower on Azure too, although the penalty was much smaller
than on the Mac. Two rounds per configuration, production preparation and
inference only; decoding, NMS, recovery detection, landmarks and embeddings
are excluded from timing. Production NMS/box validation were reused for checks:

| Detector batch | Amortized wall ms/image |
| --- | ---: |
| 1 | 73.95 |
| 2 | 76.43 |
| 4 | 80.35 |
| 8 | 84.65 |

All 384 timed detector passes retained boxes and scores exactly. Keep detection
single-image on the available evidence.

## Environment and provenance

- Deployed worker image resolved to immutable digest
  `ghcr.io/git4rajat/photostore-ipworker@sha256:190d9df9a579e8cba1f44cd82f8a15e4041351acfdcde892e3c54cca846967f5`
  (the current d91e01d build).
- A source-only overlay supplied the proposed `vision_utils.py`,
  `image_batching.py` and counter definitions. The image was not rebuilt or
  rolled out to production. SHA-256 fingerprints of every overlaid module and
  benchmark script were recorded and verified against the final local source.
- Container Apps manual job in the existing West Europe environment, 2 CPU,
  4Gi memory; verified cgroup quota `200000 100000`.
- Intel Xeon Platinum 8573C, Linux x86_64, CPU execution. Exact Python/dependency
  versions and native threadpool settings are in the adjacent JSON.
- OpenBLAS/OpenMP limits 1, PyTorch compute threads 1, ONNX intra/inter-op threads
  1. Shared decoding and two source workers match the production settings.
- All preview hashes match the local corpus and all three experiments: 48 input
  entries, 45 unique preview byte sequences, including three duplicate stills.
- CLIP checkpoint SHA-256:
  `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`.
- Successful execution `vision-clip-923ebe76bc-ix5h46c`, 22:04:34–22:11:00 UTC.
  Startup/downloads/model load/warmup are outside core timings. An initial
  execution was superseded because the module overlay changed the vocabulary
  asset's relative path; none of its results are included. The measured execution
  explicitly uses `/app/data/tag_vocabulary_embeddings.npz`.

The adjacent JSON contains per-round timings, distributions, throttling counters,
input/source hashes and output consistency checks. It contains no filenames,
image pixels, recognized text or embedding vectors. Unrelated per-image storage
counters were removed from the retained core report.

## Next experiment

Batch formation needs to happen before source workers block for CLIP results,
for example by asynchronously submitting prepared inputs while other source
steps proceed. That is a scheduling hypothesis, not a measured improvement.
Any implementation must preserve bounded memory, source/result association,
shared decode lifetime, shutdown/draining and current write fences, and must be
compared on full core wall time under the same two-CPU quota. Increasing worker
count or adding a longer synchronous wait is not supported by these results.

The bounded broker remains opt-in for experimentation. See
`docs/audits/vision-image-batching-2026-10-27.md` for configuration, telemetry and
test coverage. No default enablement is warranted by this measurement.

Temporary job and private storage were deleted; absence verified at 2026-10-27T22:12:33.353580+00:00. Production Vision remained healthy on revision 0000022 with its original image, two workers and OpenBLAS limit 1.
