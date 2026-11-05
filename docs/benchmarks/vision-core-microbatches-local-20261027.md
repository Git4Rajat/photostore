# Vision core: local microbatch experiments, 2026-10-27

CLIP image microbatching reduces CPU and amortized processing cost on this Mac.
YOLO detector batching makes the current model/runtime substantially slower.
No production processor, model, threshold, scheduler or deployment was changed.

## Method

The same 48 worker JPEG previews as the previous local/Azure experiments:
20 portrait inputs, 12 CelebA inputs, 12 video stills and four text controls.
These contain 45 distinct preview byte sequences; three stills are duplicates.
Preview SHA-256 lists match across all three new experiments. This is a small
performance corpus, not a representative production or model accuracy dataset.

macOS 26.6.2 arm64, Apple M4 Pro; CPU inference, one PyTorch compute thread,
one ONNX intra/inter-op thread, BLAS/OpenMP thread limits set to one.
Exact dependency versions, input hashes and per-round results are in
`vision-core-microbatches-local-20261027.json`. MediaPipe initializes native
graphics on this platform; the complete face path differs from Linux.

Models and all tested batch shapes were warmed. Each microbatch configuration
ran four rounds with configuration order reversed on alternate rounds. The
ordinary full core path ran three rounds with 1/2 workers and shared decoding
off/on. Experiments ran sequentially, with no competing benchmark processes.
Input reads, preview creation and model startup are excluded from timing.

## CLIP: useful throughput gain

Timings include preview decoding, the existing CLIP preprocessing, stacking,
the real image tower, normalization and the production vocabulary/tag scoring.
The offline harness injects already-computed vectors into the production tagger;
this small harness overhead is included consistently in every configuration.

Means across four rounds, milliseconds per image:

| Batch | Amortized wall cost | CPU cost | Forward + normalization | Reduction vs batch 1 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 24.18 | 24.18 | 16.38 | — |
| 2 | 21.03 | 21.03 | 13.31 | 13.0% |
| 4 | 18.69 | 18.69 | 11.16 | 22.7% |
| 8 | 17.38 | 17.38 | 9.89 | 28.1% |

All 768 timed image passes produced exactly identical float32 embeddings,
tag order, person decisions and prediction scores to the actual single-image
production encoder on this corpus. This does not establish equality on every
possible image or other hardware/runtime builds.

Decoding/preprocessing remains around 6.9–7.0 ms/image; batching saves mainly
image-tower inference. Peak process RSS was about 1.70 GB after warming all
shapes; it is a cumulative high-water mark and cannot compare batch memory use.

Amortized cost is not image latency. A batch-8 image waits approximately
139 ms for its batch to execute, versus about 24 ms at batch 1, before any
queue formation delay. The report records both distributions separately.
The current two synchronous source workers cannot naturally populate batches
of four or eight while waiting for their results. Batch 2 is the relevant first
integration experiment; increasing source workers solely to fill batches is
not justified by these results. Earlier two-vCPU Azure tests favored two workers.

## Face detector: reject batching on this runtime

This experiment includes the existing YOLO letterboxing/tensor preparation and
CPU ONNX inference. JPEG decode, output filtering/NMS, recovery detection,
landmarks and embeddings are excluded from timed sections. Production output
validation, coordinate conversion, thresholds and NMS are reused outside timing
to compare detections with batch-1 results.

| Detector batch | Amortized wall ms/image |
| --- | ---: |
| 1 | 37.46 |
| 2 | 92.17 |
| 4 | 92.09 |
| 8 | 92.54 |

All 768 timed image passes preserve detection counts/order within the stated
coordinate/confidence tolerances. Maximum coordinate difference is
0.0003663 pixels; maximum confidence difference is 0.000000358. Raw model
outputs are not bit-identical. A dynamic batch input alone does not guarantee
a fast batched execution path. The cause of the slowdown was not isolated.
Keep production detection single-image on the evidence available.

## Full core profile: where to focus

Average milliseconds per image across three rounds. Concurrent per-image
durations overlap; they must not be summed to infer throughput wall time.

| Workers | Shared decode | Face | CLIP | OCR | Wall for 48 inputs |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | off | 54.47 | 24.09 | 50.79 | 6.226 s |
| 1 | on | 50.53 | 19.97 | 51.51 | 5.875 s |
| 2 | off | 57.10 | 26.81 | 56.77 | 3.404 s |
| 2 | on | 51.91 | 21.84 | 55.98 | 3.144 s |

With one worker and shared decode, face time breaks down as follows:

| Face substage | Mean ms/image |
| --- | ---: |
| Decode/conversion | 0.68 |
| Detection | 37.92 |
| Landmarking | 1.90 |
| Alignment | 0.04 |
| Embedding | 9.96 |

Detection consumes about 75% of face time. Embedding costs about 28.1 ms per
accepted embedded face, rather than 9.96 ms for every face; the latter is averaged
over all 48 inputs, including those without faces. Each configuration accepted
17 faces across 15 images. All 576 timed full-core passes preserved exact output
hashes, excluding timing diagnostics. OCR found text on seven inputs, and warmed
OCR engine initialization time was zero. The four generated controls and small
photo corpus are insufficient to validate an OCR resolution reduction.

## Implementation direction

1. Validate CLIP batch 2 in a bounded broker with a short maximum wait, singleton
   flush and preserved per-source association. Measure the complete source pool
   under the deployed two-vCPU limit; serial forward savings alone are not a
   demonstrated end-to-end gain. Compare with the current shared-decode path,
   whose CLIP input decoding is already reused.
2. Keep YOLO single-image. The next detector experiment should evaluate a CPU
   optimized export/runtime against fixed detection and face-recall checks;
   threshold/input-size changes require a separate quality evaluation.
3. Keep OCR workers reusable. Test OCR resizing separately with labelled small
   and dense text; unchanged or reduced text is not automatically correct text.
4. Re-run promising candidates in the exact Linux worker image on two vCPUs.
   These Mac measurements do not replace the earlier Azure-matched benchmark
   and do not predict a percentage reduction in total Vision upload time.

## Reproduction

Set the environment from `vision-stages-local-20261027.md`, including the local
CLIP checkpoint, model directory, vocabulary, Tesseract data and thread caps.
Run each experiment separately with `PYTHONPATH=backend` and the worker Python:

```bash
export OMP_THREAD_LIMIT=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
python backend/scripts/benchmark_clip_microbatches.py \
  --manifest /path/to/manifest.json --output /tmp/clip.json --rounds 4
python backend/scripts/benchmark_face_detector_batches.py \
  --manifest /path/to/manifest.json --output /tmp/detector.json --rounds 4
python backend/scripts/benchmark_vision_stages.py \
  --manifest /path/to/manifest.json --face-landmarker /path/to/face_landmarker.task \
  --output /tmp/core.json --rounds 3 --workers 1 2 --shared-decode both
```

The reports contain timings, hashes and numeric output consistency checks,
not filenames, OCR text, image pixels or embedding vectors. The retained core
report omits unrelated storage metric fields from per-image snapshots.
