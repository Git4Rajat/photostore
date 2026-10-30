# INT8 face and CLIP, fixed Azure 2 vCPU / 4 GiB

**These INT8 candidates are much faster but fail the output-quality checks.
Do not deploy them.** The independent receipt-query experiment provides a
safer immediate optimization target.

## Setup

Build `704f626`, same ipworker image as Vision, temporary manual Azure job.
Actual cgroup limits: `cpu.max = 200000 100000`, `memory.max = 4294967296`.
ONNX Runtime and PyTorch kernels run on CPU with one inference/native thread;
three source callers share the production-style CLIP image/text model lock.
No managed identity, production credentials, mounts, queues or data access.

AdaFace uses 32 distinct aligned local face crops for static calibration and
64 separate held-out crops. Hash sets are disjoint, but identities are not
labeled/disjoint: this is an embedding regression test, not a recognition
accuracy benchmark. The 48-image CLIP corpus contains 45 distinct previews;
short/medium/long descriptions are synthetic, not real semantic text.

AdaFace uses preprocessed static QDQ, uint8 activations, per-channel int8
weights, quantizing Conv/MatMul/Gemm. CLIP uses shape-inferred, dynamically
quantized per-channel int8 MatMul/Gemm; embeddings/other operators can remain
FP32. FP32 reference weights and preprocessing match the earlier exports.
Graph hashes/sizes and exact runner hashes are in raw JSON.

The quantization API and preprocessing choices follow
[ONNX Runtime quantization guidance](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html).
Quantization is lossy; these particular schemes are experiments, not validated
model replacements. Results do not establish that all INT8 methods fail.

## Timing

Three alternating-order rounds, 30 comparisons. Component totals: 384 face
inferences and 432 CLIP image+text pairs. Combined kernel totals: 960 sequences,
each one held-out face followed by a CLIP image/text pair, at three callers.
All load, calibration, tensor preparation, transfer and warmup are excluded.

| Component batch | FP32 current median | INT8 median | Paired median wall reduction |
| --- | ---: | ---: | ---: |
| AdaFace, 64 faces | 8.604s | 3.145s | **63.45%** |
| CLIP, 48 image/text pairs | 8.798s | 3.473s | **60.48%** |

Face INT8 uses approximately 6.32 CPU-seconds versus 17.16 for FP32 per batch;
CLIP INT8 uses 3.47 versus 8.79. These are real CPU-work reductions, not extra
cores or merely overlapping more network requests.

| Combined kernels, 64 sequences | Median wall | Paired reduction versus current |
| --- | ---: | ---: |
| Current face + PyTorch CLIP | 14.120s | reference |
| FP32 ONNX CLIP only | 13.692s | 5.15% |
| INT8 face only | 12.479s | 11.18% |
| INT8 CLIP only | 11.459s | 18.54% |
| INT8 face and CLIP | **5.531s** | **60.83%** |

Both-INT8 paired reductions range 58.56–62.38%. FP32 ONNX's range is -0.67%
to +5.64%, still a modest/inconsistent combined-kernel improvement.

Optimizing one model shifts the bottleneck: face-only INT8's 63% component
gain becomes 11% in the combined sequence, while CLIP-only's 60% becomes 19%.
These sequences exclude OCR, face detection/alignment, image preprocessing,
artifact/persistence work and wave coordination. They are **not complete Vision
or upload throughput gains**, and cannot be applied directly to prior uploads.

## Quality: rejected

| Check versus current FP32 | Face INT8 | CLIP image INT8 | CLIP text INT8 |
| --- | ---: | ---: | ---: |
| Minimum matching-vector cosine | **0.9104** | **0.9733** | **0.7293** |
| Median matching-vector cosine | 0.9819 | 0.9925 | 0.7840 |
| Nearest-neighbor changes | **14 / 64** | **5 / 48** | **36 / 48** |

Face pairwise cosine changed by up to 0.1232; two pair decisions crossed the
0.65 test threshold. The grid is a stability diagnostic, not a complete model
of deployed clustering/curation decisions. Absence of changes at other tested
thresholds does not establish safety.

CLIP image/text retrieval top-one results changed on **46 / 48** synthetic
queries, and the combined top-15 ordering/person-decision comparison changed
on **48 / 48** images. The latter counter includes reordered tags; it does not
mean every image changed person classification or every tag changed membership.
Cross-modal scores changed by up to 0.1225. Synthetic descriptions make this
retrieval stability, not ground-truth semantic accuracy.

FP32 ONNX retains cosine above 0.999999, with no tag/person, retrieval top-one
or nearest-neighbor changes in this small test. Quantization makes the large
difference, and the naive CLIP text quantization is especially unsuitable.

Any follow-up must isolate sensitive layers, improve calibration or use mixed
precision and then evaluate labeled face identities/near-threshold cases and
real retrieval relevance. Do not reuse the current embedding compatibility
contract for an unvalidated quantized model or enable it behind a default-on flag.

## Memory and operational limits

All runs completed successfully under the 4 GiB limit. The lifetime cgroup
memory peak reached the limit; downloaded graph/weight files and filesystem
page cache are charged to that cgroup. This is not a per-variant model RSS
measurement and does not establish production memory headroom. Session loading
is excluded from timers but its resident allocations remain within the limit.
CPU throttling counters are retained per comparison. Models, fixtures and
baseline/candidate sessions use the same job and limits; no scaling change was
made to production.

Timing repeats are three per configuration, on one node/CPU model, not a fleet
study. The benchmark is an ablation to identify compute potential and quality
risks. The private temporary job and storage are removed after raw result
archival; cleanup timestamps are recorded in the adjacent JSON.

Raw [results](vision-int8-azure-20261029.json), plus
[receipt query results](vision-receipt-query-shape-azure-20261029.md) and the
[lifecycle audit](../audits/vision-artifact-lifecycle-2026-10-29.md).
