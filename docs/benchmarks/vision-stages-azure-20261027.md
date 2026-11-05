# Matched Azure Vision benchmark — 2026-10-27

The same 48 inputs run substantially slower on the deployed Linux worker than
on the Mac. Native thread limits recover measurable performance without
changing models, thresholds or model outputs on this sample. Raising source
concurrency above two makes this compute-only workload slower.

## Controlled setup

- Exact deployed image: `ghcr.io/git4rajat/photostore-ipworker@sha256:59ec742d5c371628be061aa008ad93229221fdcdb886756c6178ab0077c787a0`
  (commit `2e0ad6c6b1b51ded9d44dbf2788eb57e3577ae97`).
- Azure Container Apps manual job, West Europe, 2 CPUs / 4 GiB. Verified
  cgroup quota `200000 100000`; Intel Xeon Platinum 8573C; Python 3.11.18.
- No managed identity, production volumes, application entrypoint, queue
  access or production data access. Inputs/results in isolated private
  temporary storage; blob-scoped, short-lived SAS secrets.
- Same 48 originals as the local benchmark. All generated JPEG preview hashes
  match the local previews byte for byte. Same CLIP checkpoint SHA256
  `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`.
- Shared decode enabled. CPU PyTorch and ONNX compute threads already one;
  production `OMP_THREAD_LIMIT=1` retained. OCR pool configuration remains two
  plus its existing two spare workers. Four workers warm models before timing.
- Two measured rounds, configurations reversed in round two. Preview creation,
  image download, model loading and warmup excluded from timed batches.
- Stage calls use the image's `/app` modules, not the newer local production
  modules. The external runner only orchestrates/times processors.

## Results

Means over two rounds, processing all 48 images:

| Source workers | Image defaults: wall | Native threads=1: wall | Defaults: CPU | Threads=1: CPU |
| --- | ---: | ---: | ---: | ---: |
| 1 | 22.011 s | 18.707 s | 31.160 s | 18.766 s |
| 2 | 15.080 s | 10.804 s | 29.854 s | 20.909 s |
| 3 | 16.046 s | 12.314 s | 32.022 s | 24.234 s |
| 4 | 16.620 s | 13.393 s | 33.208 s | 26.347 s |

Native-thread experiment sets `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`,
`OMP_NUM_THREADS=1`. Two workers reduce wall time 28.4% and process CPU 30.0%.
This is a stage benchmark gain, not a measured full-upload gain.

All configurations and both experiments produce identical output hashes on all
48 inputs, including scores and vectors, excluding timing diagnostics. Each
run produces 17 accepted faces across 15 inputs and OCR text on seven inputs.
These hashes compare Linux runs; the original local report did not retain
per-image fingerprints for a cross-platform output comparison.

An additional two-round ablation changed **only** `OPENBLAS_NUM_THREADS=1`:
10.630 s wall and 20.547 s process CPU at two source workers. This recovers the
entire observed gain: **29.5% less wall time**, **31.2% less CPU**, or **41.9%
higher throughput** than defaults. All 48 fingerprints match the baseline and
the grouped thread-limit experiment. Setting the other two variables is not
supported as an additional improvement by this experiment.

A fresh interpreter in the same image reports four OpenBLAS threads each for
NumPy and OpenCV, while OpenCV's own thread setting is two. OpenBLAS-only tuning
reduces all three loaded BLAS pools (NumPy, SciPy and OpenCV) to one. The remaining
scikit-learn OpenMP pool still reports four threads; the measured gain does not
require changing it.

The defaults' two-worker run consumes about 1.98 CPU cores throughout. Three
and four workers increase both CPU work and CLIP model-lock waiting, so adding
workers does not create more native compute capacity. Cgroup throttled duration
is a scheduler statistic, not a wall-time subtraction or a direct saving.

The Mac's two-worker shared-decode batch averages 3.313 s. Azure defaults take
4.55x as long; even native-thread tuning remains slower on Azure. Identical
preview bytes establish matched inputs, not equivalent CPU architecture,
operating system, native kernels or complete application work.

## Implications

1. Pin `OPENBLAS_NUM_THREADS=1` in Vision's runtime, retaining two source
   workers initially; confirm the full application on a fresh upload. Production
   includes storage waits and semantic text inference absent from this test.
2. Preserve the existing shared decode and reusable OCR engines; they were
   active in these experiments. More OCR engine initialization work is not the
   observed steady-state bottleneck.
3. Measure production apply/persistence separately. Its previous ~603 ms/image
   cannot be eliminated by speeding up offline face/CLIP/OCR. The newer semantic
   batching/reuse and phase instrumentation commit was not part of this image.
4. CLIP image microbatching merits a separate correctness/performance test.
   AdaFace input shape is fixed `[1,3,112,112]`; face embedding batching requires
   an independently validated export. YOLO's input batch dimension is dynamic.
   Changing batching, precision, providers or models is not validated here.

The adjacent JSON retains distributions, CPU/cgroup deltas, runtime details,
preview hashes and output fingerprints. Full per-image counter dumps remain in
`/tmp/photostore-matched-benchmark/` during this session. The reusable runner is
`backend/scripts/benchmark_vision_stages.py`; use `--workers 1 2 3 4
--shared-decode on --warmup-workers 4 --rounds 2`, and optionally
`--native-threads 1`. Set `PYTHONPATH=/app` inside the exact worker image and
provide the same manifest, model and output paths as the local benchmark.

All three executions succeeded. Temporary benchmark job and storage account
were deleted and their absence verified. Production apps, queues, permissions
and configuration were not changed.

## Runtime configuration change

`backend/ipworker.Dockerfile` now defaults `OPENBLAS_NUM_THREADS=1` before any
Python imports. The Vision container in `deploy/resources.bicep` explicitly sets
the same value, with the generated deployment template kept in sync. Source
concurrency remains two. This prepares the next build/deployment; no live app
configuration was changed. Full-upload savings still require a fresh run.

### Applied environment setting

On 2026-10-27 at 17:55 UTC, Vision revision `0000021` was verified ready and
healthy with `OPENBLAS_NUM_THREADS=1` and two source workers. Only the new
revision is active. Current image `2e0ad6c`, all other template fields, app
configuration, identity and existing storage roles match the preflight snapshot.
This applies the environment override directly; local Dockerfile defaults and
newer Python changes still require a future image build/rollout. No upload or
queue wake was triggered. Native-library introspection via CLI exec was not
confirmed because of local terminal errors; full-upload timing remains pending.
