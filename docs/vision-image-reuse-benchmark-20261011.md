# Vision image reuse and OCR benchmark — 2026-10-11

No tested option currently meets both **more than 40% less time** and
**equivalent results**. Production configuration and image visibility were
not changed. Both image-reuse experiments default to off; Tesseract remains
the OCR engine.

| Experiment | Baseline batch | Candidate batch | Time reduction | Output comparison |
| --- | ---: | ---: | ---: | --- |
| One RGB decode across OCR, faces and CLIP; 153 inputs, two workers | 15.609 s | 14.693 s | 5.87% | Identical on 153/153 inputs, excluding timing diagnostics |
| Existing JPEG preview versus original RAW; four inputs, one worker | 2.264 s | 0.425 s | 81.23% | Tag sets changed on all four; not equivalent |
| English mobile PP-OCRv5 through RapidOCR versus Tesseract; 29 inputs, one worker | 3.344 s | 17.522 s | -424.00% (5.24 times slower) | Text differed on 21/29; six labelled controls scored 6/6 for Tesseract, 5/6 for the candidate |

These are local, warmed **in-memory processing** measurements. They do not
measure Azure blob transfer, queue delay, EXIF, thumbnail generation, result
persistence, clustering, indexing, or application cold start. The final shared
decode run and the RAW/OCR comparisons ran without a concurrent test suite.

## What was already implemented

The preview-first runner already replaced original bytes with its generated
JPEG for subsequent visual steps. Each OCR, face and CLIP processor still
decoded those bytes separately. When preview processing had already completed,
the runner fetched the original again instead of downloading the stored preview.

## Changes prepared locally

- `IPWORK_SHARED_DECODE=false`: opting in scopes one oriented RGB image to one
  photo job. OCR, face detection and CLIP reuse that image. The cache closes
  its image on source changes and every job exit, including exceptions. Jobs
  on different threads have separate caches. No inference model was changed.
- `IPWORK_REUSE_PREVIEW=false`: opting in lets outstanding visual steps fetch
  the stored JPEG when metadata reports `preview_status=done`, preview
  generation is not requested, the input is not a video, and no original has
  already been downloaded. The original is HEADed for source-size accounting.
  Missing, invalid or truncated previews fall back to the original.
- Original and visual byte caches are separate. EXIF/GPS steps receive the
  original even after preview generation or reuse; they need its metadata.
- Processors now recognize JPEG bytes before applying filename-based RAW
  extraction. A preview passed under a `.CR3` filename previously entered
  the RAW extraction path again. In the pilot, this made preview processing
  unnecessarily slow. The corrected RAW comparison above includes this fix.
- Metrics distinguish original download bytes, preview download bytes, logical
  productive source bytes, successful preview reuse and failed reuse. Throughput
  logs include the two experiment flags and preview/total download rates.
  The existing `download` duration includes preview validation when attempted.
- Results still persist as one completed processing payload. No photo is made
  visible early, and minimum replicas were not changed.

The two flags, benchmark harness and tests are source changes only. No image
was built or deployed and no production storage data was written.

## Corpus and quality limits

Shared decoding used all 146 cached real photos from the existing face
validation corpus, six synthetic text controls and one EXIF-orientation control.
The baseline produced OCR text on 18 inputs, accepted 93 faces across 79 inputs,
and generated CLIP results for all 153. Both crossover rounds matched every
non-timing result exactly, including face embeddings and CLIP embeddings.
A separate 24-photo pilot measured 7.32% less time with identical results.

The RAW comparison used four local Canon CR3 files. Together their originals
were **54,048,840 bytes**, versus **349,685 bytes** of generated previews:
**99.35% smaller input payload**. This is a byte-size comparison, not measured
Azure download latency. OCR was empty on all four and neither path accepted
a face, so this sample cannot establish OCR recall or face quality. CLIP
embedding cosines ranged from 0.99537 to 0.99937, but tag label sets changed
on every sample. Re-encoding a preview can change model input pixels even
when dimensions stay the same. This prevents adoption under the agreed parity
condition.

The final OCR comparison used eleven real photos where the current engine
found text, twelve photos where it did not, and six controls with known text.
The candidate used RapidOCR 3.10.0, PP-OCRv5 mobile detection and English mobile
recognition, CPU ONNX Runtime with one intra/inter-op thread, and its default
0.5 text score. Tesseract kept its current per-word confidence threshold of 40.
Those confidence scales are not interchangeable. Extra candidate text on real
photos has not been independently labelled; output differences alone do not
prove which engine is more accurate. One synthetic control gained a spurious
`F` in the candidate output. This configuration fails both speed and parity;
the result does not rule out every PaddleOCR configuration or other OCR engines.

The host was an Apple M4 Pro, macOS 26.6.2 arm64, Python 3.13.2. Runtime versions:
Pillow 12.3.0, tesserocr 2.8.0, torch 2.13.0, ONNX Runtime 1.28.0,
open_clip_torch 3.3.0. The real YOLO, MediaPipe, AdaFace and OpenAI CLIP
checkpoints were used. Torch and ONNX compute threads were bounded, but the
host was not constrained to Azure's two-vCPU replica limit; MediaPipe used its
macOS graph implementation. Linux/Azure performance and peak memory remain
unmeasured. First-inference/model-load timings were separated and are not
valid cold-start A/B comparisons.

## Reproduction

`backend/scripts/benchmark_ipwork_images.py` accepts a local JSON manifest:

```json
[{"path": "/absolute/path/photo.jpg", "filename": "photo.jpg"}]
```

Optional `expected_text` labels support OCR controls. The harness reads local
inputs, generates the same preview as the worker, warms both modes, alternates
their order, and saves timings and outputs locally. It never imports the app
or writes Azure storage. Use an environment with the ipworker dependencies:

```bash
PYTHONPATH=backend python backend/scripts/benchmark_ipwork_images.py images \
  --manifest /absolute/path/manifest.json --output /tmp/shared.json \
  --limit 153 --rounds 2 --workers 2 \
  --face-landmarker /absolute/path/face_landmarker.task
```

Set `IPWORKER_MODELS_DIR`, `IPWORKER_VOCAB_PATH`, `OPENCLIP_PRETRAINED` and
`TESSDATA_PREFIX` for local models. Use the correct `.safetensors` suffix when
pointing at a cached safetensors checkpoint. Run `preview` instead of `images`
to compare original inputs against stored-preview pixels. Run `ocr` with one
worker, isolated RapidOCR dependencies and `--ocr-model-root /tmp/ocr-models`
for the OCR comparison. Candidate dependencies/models were installed only under
`/tmp/photostore-vision-benchmark`; production requirements were not changed.

Private manifests and per-photo outputs are retained under that temporary
directory, rather than committed to the repository. Final result files:
`shared-decode-clean.json`, `raw-preview-corrected.json`, `ocr-balanced.json`.

## Validation and next decision

**295 relevant tests passed**, covering worker execution, dispatch, leases,
retry/shutdown handling, metrics, RAW/thumbnail fallbacks, shared decode
isolation/cleanup, preview corruption/missing data and original metadata input.
`git diff --check` also passed.

Keep both experiment flags off. Shared decoding appears safe on this corpus
but offers a modest local gain. Preview reuse offers the larger opportunity;
before enabling it, establish a canonical visual input for fresh and resumed
processing, then replay a larger RAW corpus with real text and faces. Compare
OCR recall, tag labels, face acceptance and clustering, plus end-to-end latency
and memory on the existing Azure replica size. Adoption needs more than 40%
less relevant time with equivalent accepted results.
