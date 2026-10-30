# Independent review and CPU benchmarks — 2026-10-29

## Review of the supplied local report

The receipt-reuse conclusion is supported by the raw data: all paired local
receipt-only comparisons were faster. Cross-image face batching remains
unproven; reducing request count did not consistently reduce elapsed time.
These component results must not be presented as full-upload gains.

Two limitations matter for the model results:

- Configurations A/B change source workers and native-thread environment
  together. They do not establish the independent benefit of either variable.
  The stage runner also explicitly pins PyTorch to one thread on import and
  production ONNX sessions to one thread. Native environment values alone do
  not demonstrate changed PyTorch/ONNX execution thread counts.
- The approximately 10.1ms embedding mean includes zero-face images. The corpus
  contains 17 embedded faces across 15 of 48 inputs; the measured embedding cost
  is approximately 28.5ms per embedded face. Comparing 10ms against a mostly
  face-positive Azure upload exaggerates the apparent hardware/runtime gap.
  Mac MediaPipe graphics initialization does not imply that AdaFace or CLIP
  themselves used GPU: the independent measurements explicitly use ONNX's CPU
  provider and PyTorch's CPU device.

An Azure receipt/query comparison does not require production data. A private
temporary account with representative populated partitions can test transport
and SDK behavior; production credentials/data are not intrinsically necessary.

## Independent experiment design

All measurements are local to the same Apple M4 Pro host; no deployed settings,
production data, cloud resources or project dependency versions were changed.
The ONNX export dependency was installed under `/tmp` only. Native MediaPipe
initialization requires execution outside the filesystem sandbox on this Mac.

The same 48-input corpus has 45 distinct preview byte sequences. Face-runtime
tests use 17 actual aligned crops captured from the production face processor.
CLIP tests use production preprocessing and the existing model weights plus
distinct synthetic short/medium/long descriptions, not private semantic text.

Three complementary comparisons:

1. AdaFace ONNX thread counts 1/2/4, spinning on/off and graph optimization
   all/extended at one and three concurrent callers. CLIP image+text inference
   compares `no_grad`/`inference_mode`, PyTorch threads 1/2/4 and one/three callers,
   retaining the shared model lock.
2. Whole model-processing replay: production OCR, face and image-tag processors
   with shared decode, followed by real CLIP text encoding. Mixed profile has
   48 inputs; face-positive profile repeats the 15 positive inputs four times
   (60 passes). No persistence, artifacts, download, queue or wave coordination.
3. Same-weight FP32 CLIP image/text ONNX export versus PyTorch, retaining the
   normalized 512-dimensional vector space. Export, load, preprocessing and
   warmup are excluded from timing. Legacy export disables native attention
   fastpath only during export; the PyTorch baseline keeps its default path.

Final runs explicitly set `OMP_THREAD_LIMIT=4` for every compared variant and
`OMP_NUM_THREADS`, OpenBLAS, MKL and vecLib limits to one before native imports.
This allows actual PyTorch 2/4-thread candidates rather than silently capping
them at one. It differs from production's OpenMP limit and has no two-vCPU quota;
no local result establishes deployed throughput or an Azure configuration change.
Earlier exploratory runs capped at one, including warning-producing variants,
are superseded and excluded from the final result files.

CLIP configurations and full-processor configurations alternate order by round.
AdaFace configurations run in fixed order, alternating caller order per round;
small differences between AdaFace configurations may include order effects.
All timings are elapsed or process CPU as labeled, never additive exclusive
partitions. Per-item amortized wall is batch wall divided by completed inputs.

Quality checks compare finite vectors, cosine similarity, absolute error,
pairwise similarity and nearest-neighbor stability. The processor replay also
checks exact face/OCR output hashes and tag/person decisions. ONNX comparisons
add image/text retrieval top-one agreement and production-vocabulary tag/person
agreement. These are regression checks on a small corpus, not a general accuracy
evaluation or permission to change embedding compatibility/version contracts.

## Measured model-processing results

Three rounds, six configurations, two profiles: **36 comparisons / 1,944 full
model-processing passes**. Values below are paired median wall reductions versus
three current source workers in the same profile and round. Positive is faster.

| Candidate, three source workers | Mixed | Face-positive |
| --- | ---: | ---: |
| CLIP inference mode | +0.39% | +0.64% |
| Two PyTorch threads | -3.61% | -3.00% |
| Two AdaFace ONNX threads | -4.02% | -6.71% |

Inference-mode ranges cross zero: -1.10% to +0.82% mixed, -0.23% to +1.66%
face-positive. This is not a useful consistent improvement. All face/OCR output
hashes, tag/person decisions and embedding vectors match the controls.

Current one/two/three-worker median walls are 6.565/3.527/2.729 seconds for
48 mixed inputs and 8.851/4.752/3.551 seconds for 60 face-positive passes.
This unconstrained host can use more than two cores; these scaling results must not
be transferred to the deployed two-vCPU allocation.

Component experiments add **66 comparisons**: 30 AdaFace runs / 510 face
inferences, and 36 CLIP runs / 1,728 image+text pairs. AdaFace median amortized
wall per crop with three concurrent callers is 13.16ms at one native thread,
15.63ms at two and 14.03ms at four. Single-caller two/four-thread improvement
costs much more CPU and disappears under concurrent callers. Extended rather
than all graph optimizations slows the three-caller result to 20.20ms.
Disabling spinning gives a small 2.2% ratio-of-medians component reduction;
fixed configuration order and no full-processor confirmation make this
insufficient evidence for a production change.

At three callers CLIP image+text pair medians span approximately 27.9–28.6ms
across settings. One-thread/no-grad uses 28.64 CPU-ms per pair; four-thread/no-grad
uses 36.39 CPU-ms for similar elapsed time. More threads are not free performance.

Raw results: [components](vision-next-gains-independent-components-20261029.json),
[model-processing replay](vision-next-gains-independent-pipeline-20261029.json).

## Equivalent CLIP ONNX component result

Three rounds / six configurations / **864 image+text pairs**. Same FP32 weights,
normalization and preprocessing; batch size one. Median pair costs:

| Runtime | Wall ms/pair | Process CPU ms/pair | Paired wall reduction |
| --- | ---: | ---: | ---: |
| PyTorch, one thread | 27.49 | 27.49 | reference |
| PyTorch inference mode, one thread | 27.47 | 27.46 | +0.06% |
| ONNX, one thread | 26.05 | 26.05 | **+5.23%** |
| ONNX, two threads per session | 23.57 | 61.39 | +14.44% |
| ONNX, four threads per session | 16.65 | 113.27 | +39.77% |

One-thread ONNX improves every paired round, range 4.79–6.04%. Two/four-thread
elapsed gains consume approximately 2.2x/4.1x the baseline process CPU per pair;
multiple session thread pools can also spin between image/text calls. The
four-thread case averages about 6.8 cores during its measured interval. This is
not an applicable 40% saving for a two-vCPU application.

Every candidate preserves all top-15 vocabulary tags and person decisions on the
48 inputs; image/text retrieval top-one results and within-modality nearest
neighbors are unchanged. Minimum ONNX cosine similarity is above 0.999999;
maximum embedding absolute error is below 0.000005. This is small-corpus numeric
regression evidence, not broad retrieval/recognition accuracy validation.

Raw results: [same-weight ONNX comparison](vision-next-gains-independent-onnx-20261029.json).

## ONNX inside the full model-processing replay

Three additional rounds / three configurations / two profiles: **18 comparisons
/ 972 processing passes**. Production decoding, OCR, face pipeline, CLIP
preprocessing, tags and synthetic semantic-text encoding remain in the measured
sequence. The benchmark substitutes only the singleton image/text inference
helpers, retaining their shared model lock and instrumentation. All variants
run three source workers. Graph/session load and processor warmup are excluded.

| CLIP runtime | Mixed paired wall reduction | Face-positive | Process CPU change |
| --- | ---: | ---: | ---: |
| ONNX, one thread | **4.33%** | **4.35%** | -1.7% / -2.5% |
| ONNX, two threads | 6.38% | 5.24% | +20.1% / +21.5% |

One-thread paired ranges are 0.16–6.64% mixed and 3.43–4.43% face-positive.
Median batch walls: mixed 2.734s PyTorch versus 2.615s ONNX-one-thread; face
positive 3.518s versus 3.362s. All exact face/OCR hashes and tag/person decisions
match; image/text vectors retain cosine similarity above 0.999999. These checks
do not replace production-size retrieval/identity evaluation.

The one-thread candidate saves wall time without extra CPU on this host and is
the most useful model-runtime candidate from these experiments. Two-thread
ONNX's smaller extra benefit consumes more CPU and reaches 3.5–3.7 average cores
in the whole replay, incompatible with assuming the same result on two vCPUs.

Raw results: [ONNX processor replay](vision-next-gains-independent-onnx-pipeline-20261029.json).

## Decision

**Carry one-thread FP32 ONNX CLIP forward to a matched Linux/two-vCPU benchmark;
do not deploy from these local results.** It is a modest improvement, not a
solution to the 33% live wall-time gap. Current AdaFace/PyTorch thread counts
and CLIP inference context should remain unchanged based on this evidence.
Receipt reuse remains a separate candidate supported by prior local and Azure
component tests; naive cross-image persistence batching remains unproven.

Across the final experiments: **138 comparisons**, 510 AdaFace crop inferences,
2,592 CLIP image+text pairs and 2,916 whole model-processing passes. Four quality
helper tests pass. The reusable runners are
`backend/scripts/benchmark_vision_cpu_runtime.py` and
`backend/scripts/benchmark_clip_onnx_runtime.py`. The first runner was extended
with the optional ONNX replay after the earlier component/replay runs; raw JSON
records the runner hash at archival. Graphs and temporary dependencies remain
under `/tmp`; no production modules/configuration were modified.
