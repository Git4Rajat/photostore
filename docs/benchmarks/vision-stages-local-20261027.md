# Local vision stage benchmark — 2026-10-27

The reported 389 ms face / 164 ms CLIP / 38 ms OCR averages are plausible
deployment observations, but they are not inherent minimum model costs. Current
local CPU runs are substantially faster for face and CLIP. This does not establish
the speed attainable on the deployed two-vCPU worker.

## Measured results

48 inputs: 20 files from `hq50k_images`, 12 from `img_celeba`, 12 video preview
stills, and four generated text controls. Each group uses sorted filenames.
Inputs are converted to the worker's JPEG previews before timing, then held in
memory. This sample is not the production upload corpus or a quality evaluation.

Real YOLO, MediaPipe, AdaFace, CLIP ViT-B-32-quickgelu and Tesseract ran through
the current processor functions. Models and OCR engines were warmed first.
CLIP used CPU with one PyTorch compute thread; ONNX sessions use the existing
single-thread configuration. macOS 26.6.2 arm64, Python 3.13.2. Dependency
versions and detailed distributions are in the adjacent JSON.

Means below average two rounds, with configuration order reversed in round two.
Durations are elapsed time per image, not CPU consumption. Concurrent durations
overlap and must not be summed to estimate batch wall time.

| Workers | Shared decode | Face | CLIP | OCR | 48-image batch |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | off | 56.33 ms | 25.42 ms | 53.97 ms | 6.534 s |
| 2 | off | 58.66 ms | 27.81 ms | 57.29 ms | 3.481 s |
| 1 | on | 53.84 ms | 22.23 ms | 55.66 ms | 6.342 s |
| 2 | on | 54.42 ms | 23.52 ms | 59.07 ms | 3.313 s |

All eight configurations/rounds produced exactly matching result hashes on all
48 inputs, excluding timing diagnostics. Each produced 17 accepted faces across
15 inputs and OCR text on seven inputs. Normal quality rejections count as valid
processing; inference/runtime errors abort the benchmark.

Single-worker, separate-decode face average: decoding 5.15 ms, detection
38.95 ms, landmarking 1.94 ms, alignment 0.04 ms, embedding 10.20 ms.
These are averages over all images, including those without faces; per-face and
crowd-photo costs will differ. CLIP inference itself averaged 17.23 ms,
preprocessing 2.99 ms, with effectively no model-lock wait. Remaining CLIP
processor time includes decoding, normalization and vocabulary scoring.

Shared decoding reduced batch wall time by 2.9% with one worker and 4.8% with
two workers. Two workers improved batch throughput by 1.88x without shared
decoding, while increasing each image's elapsed processing time slightly.
The host was not constrained to two vCPUs. Production already configures two
workers; this is not evidence that adding workers there will improve throughput.

Initial CLIP/vocabulary loading took 1.822 s; a separate 12-input warmup took
1.863 s. These are not complete application/container cold-start measurements.
No download, preview generation, EXIF, queueing, apply/persistence, clustering or
indexing time is included in the table. MediaPipe on this Mac initializes native
graphics services, so the entire face path is not a Linux CPU-only replica.

## Interpreting the reported deployment timings

- Faces and CLIP can run much faster locally, but hardware, CPU quota, input
  dimensions, face counts and contention must be matched before attributing the
  difference to avoidable overhead. The current instrumentation separately
  captures face substages, CLIP preprocessing/inference/lock wait, and OCR
  initialization/recognition. Compare these on a fresh deployed run.
- OCR at 38 ms is consistent with inexpensive images; text-heavy inputs can be
  much slower. The local corpus and four text controls averaged about 54 ms.
  Warmed OCR engine initialization was zero in every measured round.
- Applying/persisting results at 603 ms is the largest supplied average. It
  includes semantic text embedding inference as well as face/embedding writes,
  artifact capture/promotion, metadata and completion validation. An offline
  model benchmark cannot establish its real storage latency or attainable gain.
- Shared decode is a modest gain here and is already conditional on
  `browserUploadPreparation` in deployment configuration. Verify the deployed
  flag before treating it as an available new optimization.

The next useful comparison is the same inputs on the deployed worker, using
the stage counters and apply-phase diagnostics. In particular, distinguish
CLIP lock waiting from forward inference, semantic text work from storage I/O,
and request-count savings from merely moving work outside a timer. This run
does not justify changing models, quality thresholds or worker concurrency.

## Reproduction

The new `backend/scripts/benchmark_vision_stages.py` accepts the same manifest
format as the existing image benchmark: a JSON array of objects containing
absolute `path` and optional `filename`. It does not import the application or
write storage. It saves hashes instead of OCR text, image contents or embeddings.

This run used:

```bash
OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 \
MPLCONFIGDIR=/tmp/photostore-stage-benchmark/mpl \
TESSDATA_PREFIX=/opt/homebrew/share/tessdata \
IPWORKER_MODELS_DIR="$PWD/frontend/public/models/browser-ai/models" \
IPWORKER_VOCAB_PATH="$PWD/frontend/public/models/browser-ai/vocab/tag-vocabulary.v1.json" \
OPENCLIP_PRETRAINED=/tmp/photostore-vision-benchmark/clip.safetensors \
PYTHONPATH=backend .venv-photostore-tools/bin/python \
  backend/scripts/benchmark_vision_stages.py \
  --manifest /tmp/photostore-stage-benchmark/manifest.json \
  --face-landmarker /tmp/photostore-vision-benchmark/face_landmarker.task \
  --output /tmp/photostore-stage-benchmark/results.json
```

The checkpoint symlink and downloaded MediaPipe task are temporary model assets;
provide equivalent local paths if they have been removed. On this Mac the
sandbox prevented MediaPipe graphics initialization; the successful run used
approved execution outside the sandbox. Production code/configuration was not
changed by this benchmark. Other pre-existing workspace edits were left intact.
