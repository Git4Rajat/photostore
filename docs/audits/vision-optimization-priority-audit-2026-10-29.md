# Vision optimization priority audit — 2026-10-29

## Conclusion

We have identified expensive components, but the latest write-batching work was
too narrow to be a leading route to 10k images/hour. The next experiments should
target **serial post-source qualification and admission overhead**, **overlap
between model work and apply**, and **actual same-model CPU execution cost**.
Face embedding remains the largest individual inference target. It is not,
by itself, evidence of the largest full-upload improvement.

The live baseline needs **35.4% higher throughput**, equivalent to **26.2% less
wall time**. Several component improvements will be needed unless a pipeline
experiment demonstrates a substantial combined gain. No such gain is proven yet.

This audit is read-only. No code paths, deployments, roles, data or runtime
settings were changed. The new files contain analysis and aggregate evidence.

## Evidence and baseline

Primary baseline: upload wave `91ecc876867e45f695bae557b07e4bb0`, 1,001 images.
Fresh Log Analytics queries returned 1,001 distinct image summaries, all successful
and receipt-qualified, plus aggregate apply timing/CPU data. Five retained attempt
summaries reconcile to exactly the same 1,001 images. Independent read-only table
queries examined semantic digests and actual text-tokenizer repetition.

Live configuration was rechecked: Vision revision `0000036`, image `32b9e9b`,
2 CPU / 4Gi, three source workers. Provisioning state is Succeeded.

| Measurement | Latest completed upload |
| --- | ---: |
| Vision start → frozen readiness | 13:40:07.587 → 13:48:15.562 UTC |
| Vision elapsed time | 487.975s, about 8m08s |
| Successful images/hour over that interval | 7,384.8 |
| Target elapsed time for these 1,001 images at 10k/hour | 360.36s |
| Required elapsed reduction | **127.615s** |
| Source worker busy duration, mean | 1,023.1ms/image |
| Model-step stage, mean / p95 | 474.8 / 646.1ms/image |
| Apply stage, mean / p95 | 493.0 / 632.4ms/image |
| Main-process CPU cost | 599.4 CPU-ms/image; average 1.23 cores |
| Storage throttling / server errors / source errors | 0 / 0 / 0 |

The interval excludes intake/upload and pre-start waiting. It is a whole Vision
stage rate, not a peak-minute rate or a controlled A/B against the older 9k wave.
The latest library is smaller and its face distribution differs. Main-process
CPU includes its native threads and the current resident OCR worker threads.
Those OCR workers are not subprocesses. Process CPU excludes any child processes
if present; container measurements are still preferable for total CPU capacity.

## Finding 1: a serial qualification barrier is a substantial wall-time target

The five attempts contain the following non-overlapping outer intervals:

| Interval | Seconds | Share of attempt wall time |
| --- | ---: | ---: |
| Source processing pool | 388.862 | 79.7% |
| Post-pool qualification/settlement | **73.968** | **15.2%** |
| Remaining preparation, commits, evaluation and other work | 24.818 | 5.1% |
| Attempt wall total | 487.648 | 100% |

The roughly 0.33s difference from lifecycle elapsed time is between measured
attempt intervals. The 73.968s is repeated qualification throughout the wave,
not a 74-second final tail and not a sum of concurrent source durations.

Code confirms that `_process_vision_group` drains source futures and then walks
the planned members serially. For each runnable source, it checks deletion and
supersession state, rereads metadata, finds the completion inbox, and verifies
immutable manifests/snapshots. Mean per-image qualification is 72.4ms; p95 is
134.3ms. It is safe to investigate this without changing inference quality.

The pre-source path already has bounded metadata/proof/inbox/receipt prefetch.
The post-source path discards the pre-source inspection snapshot, correctly,
but does not construct an equivalent fresh post-source batch. Reusing old
absence/policy observations would be wrong; fetching a **new** bounded batch or
performing fresh qualification with bounded parallel reads is the experiment.

Benchmark current qualification versus (a) fresh post-source metadata and
namespace-separated evidence prefetch, and (b) bounded parallel point reads.
Measure both fresh small and populated large library partitions. Preserve source
identity, deletion/replacement handling, cleanup fencing, original ETags and final
freeze checks. Inject a replacement/deletion during qualification. Do not infer
qualification solely from model completion or a locally cached successful count.

**Opportunity size:** cutting this interval in half would save approximately
37s per 1,001 images, or 7.6% of current wall time. That is a budget illustration,
not a measured improvement. Eliminating all 74s would still leave the run short
of 10k/hour, so this is a strong first experiment rather than the whole answer.

Source: [group processing and settlement](../../backend/upload_wave_execution.py),
[receipt and inspection batches](../../backend/wave_source_batch.py).

## Finding 2: admission is usually one image at a time, but its timer overlaps work

There are **885 admission batches for 1,001 images**, or 1.13 images/batch, over
52 processing groups. Admission/inspection/reservation consumes **67.886s inside
the source-pool interval**. The coordinator then spends 320.963s waiting for
futures while those workers process photos.

These are not 68s + 321s of additional waste. Admission can overlap existing
workers, and waiting for productive futures is expected. Treating coordinator
wait as idle time would mis-rank the bottleneck.

The relevant source-slot occupancy is **87.8%**: 1,024.159 busy worker-seconds
divided by 1,166.586 available worker-seconds within the pool. Unused capacity is
142.427 worker-seconds, equivalent to 47.476 wall-seconds at three workers. That
includes refill delays and page-tail imbalance; it is not 47 seconds of proven
removable wall time and not necessarily idle CPU.

Benchmark better refill/admission and smaller drain barriers using the complete
coordinator, including real admission writes. Keep attempt admission durable
before execution and avoid charging attempts to unstarted page tails. Add
separate inspection versus reservation timing and count the admitted batch sizes
so any improvement can be attributed correctly.

The legacy queue-task slot utilization metric does not measure these photo
workers. Do not use the older audit's 44% figure to infer photo-worker capacity.

## Finding 3: apply is mixed compute and storage, and models occupy source slots

Each source worker currently claims a source, runs model steps, applies results,
and releases it synchronously. Apply costs 493ms/image, approximately as much
elapsed time as the model-step stage. That makes a bounded compute-to-apply
handoff a meaningful architectural hypothesis.

However, **apply is not purely I/O**. Its measured subphases consume at least
141.8 thread CPU-ms/image. Semantic refresh alone is 102.8ms elapsed and
64.4 thread CPU-ms/image. It uses the same CLIP model lock as image encoding.
Giving apply a larger pool can increase CPU/model contention as well as overlap.

Compare the current three whole-source workers with two compute workers plus
1/2/3 bounded apply workers on the same two CPUs. Include real semantic refresh,
Azure persistence, coordinator admission and settlement. Keep in-flight count
and byte limits, source heartbeats, source/result association and durable
completion semantics. Measure useful completions and apply-queue drain, not
inferences submitted. A growing apply backlog is not a throughput win.

This differs from splitting OCR/face/CLIP into separate stage queues, which
already regressed in Azure benchmarks. Those arrangements should not be retried
unchanged under a new name.

Source: [synchronous wave source adapter](../../backend/wave_runtime.py),
[semantic refresh](../../backend/storage_utils.py).

## Finding 4: the 475ms figure omits semantic encoding in apply

Current mean leaf elapsed timings:

| Work | ms/image | Parent / interpretation |
| --- | ---: | --- |
| Face embeddings | **182.7** | Model steps; 981 images have nonzero embedding time |
| CLIP image inference | **117.2** | Model steps; excludes its separately measured lock wait |
| Semantic text inference | **86.8** | Apply, not model steps |
| Face detection | **83.2** | Model steps |
| CLIP image lock wait | 26.8 | Model steps; contention, not inference |
| OCR recognition | 26.9 | Model steps; p95 105.7ms |
| Face landmarks | 16.1 | Model steps |
| Text model lock wait | 11.8 | Apply; contention, not inference |

Face/CLIP timings are wall measurements under concurrent execution; CPU scheduling
can affect them even inside an inference call. They are not uncontended inference
CPU costs. Semantic request elapsed is 101.1ms/image including its nested inference,
tokenization and wait. Do not add those children to the request total again.

The model-step stage plus semantic request is approximately **576ms/image of
model-related elapsed work**, across two parent stages. It is not 576ms of
exclusive CPU or whole-wave wall time.

Face embedding is the first core-model target. Audit the current fixed singleton
AdaFace input before proposing batches 2/4/8: a validated batch-capable export is
needed, rather than just changing a batch-size flag. Compare isolated and
concurrent same-weight execution, actual inference versus preprocessing, and
provider/kernel/thread configuration. Require face-vector and identity-matching
checks; smaller models or lower precision require a separate quality decision.

CLIP image and text execution should be considered together: they spend about
204ms/image inside the shared model lock, plus roughly 39ms/image waiting for it.
That wait is material but is not a promise of savings from removing the lock.
Benchmark same-model CPU execution efficiency before revisiting scheduling.

For scale: halving the face embedding timer removes roughly 91.4 aggregate
worker-seconds over this wave. Spread perfectly over three workers, that is about
30.5 wall-seconds. This fixed-other-cost illustration explains why even a large
face-only component gain need not close the 128s full-stage gap. CPU contention
and changed scheduling can alter the actual result; measure the whole pipeline.

OCR is not a leading aggregate target in this upload. A slow dense-text cohort
can still matter to p95 and other workloads, but the prior 168ms OCR figure is
not representative of this warmed run.

## Finding 5: exact semantic reuse has no opportunity in this upload

All 1,001 authoritative `photoembeddings.semanticTextDigest` values are present
and unique. The live summaries report 1,001 semantic encodes and zero reuses.

A second read-only audit used the production model's tokenizer on the stored
semantic text. All **1,001 padded/truncated token inputs are unique too**. The
context length is 77; only one input exceeds the 75-token content budget. This
rules out both full-text caching and exact-token-input caching as meaningful
new-upload savings for this sample. No texts, token IDs or vectors were retained.

Existing same-source reuse can still help retries/reprocessing. Other libraries
may have repeated semantic inputs, but do not prioritize a new cross-image cache
without measuring that workload's repetition first. Do not replace distinct
texts with approximate shared vectors just to improve cache hits.

## What to stop prioritizing

- **Cross-image write batching alone:** 128 Azure comparisons found no reliable
  standalone gain after preserving parallel guards. Naive batches were slower;
  eight-face batching increased reads. Keep the existing multi-face path.
- **Receipt reuse as the main 10k strategy:** useful small optimization, but its
  2–8% single-face and approximately 17% eight-face results apply to a storage
  component, not the whole upload. Implement when convenient; do not extrapolate
  them into equivalent whole-wave gains.
- **Repeated semantic caching for fresh images:** no measured repetition here.
- **YOLO batching:** previous matched Azure tests made it slower.
- **Synchronous CLIP microbatching or unchanged async model scheduling:** prior
  Azure full-core tests regressed despite standalone CLIP batch gains.
- **More metadata buffering, disk buffering or preview-prefetch tuning for speed:**
  page loading is now about 2.5ms/image; preview-prefetch wait was negligible.
  Capacity work can still be useful for very large waves, but it is not today's
  leading steady-state throughput target.
- **Fixing throttling without evidence:** none was recorded in this run.

## Ordered benchmark plan

1. **Fresh post-source qualification plus admission profiling.** Benchmark the
   complete coordination path, including the existing 74s serial qualification
   cost and real source-attempt transactions. Start with an unchanged model path.
2. **Bounded compute/apply handoff.** Use the current three-source pipeline as
   control. Compare two compute workers plus bounded apply workers with the same
   resources, real models, semantic text and storage. Include qualification.
3. **Same-model face and CLIP/text runtime efficiency.** Use measured core profiles
   and warm, matched inputs. AdaFace is the largest single component. Validate
   a batch-capable export before batching faces. Do not change thresholds or
   recognition semantics during a scheduling/performance comparison.
4. **Four-CPU diagnostic control**, subject to environment capacity. Compare the
   unchanged pipeline and winning handoff on 2 versus 4 CPU, with bounded worker
   counts. Report useful images/hour and resource cost per 1,000 images. This
   determines whether further algorithm work is needed or capacity is the more
   practical route; it is not a recommendation to resize production now.

Use at least a 1,000-image face-heavy selection resembling this upload, plus a
mixed/no-face/text-heavy control and a 20-image latency case. The old 48-image
core corpus had only 15 face-positive inputs, versus 981 embedding-positive
images in this live wave; mixed-corpus improvements cannot be assumed to transfer.
Reuse identical input selections across variants, alternate order, warm models,
and test populated library partitions as well as fresh ones.

Primary result: useful durable source completions/hour and full Vision readiness
time. Record p95, in-flight queue growth/drain, batch occupancy, peak memory,
process **and container/child CPU**, throttling, model-lock wait, storage calls,
SDK tails and correctness. Count all successful work through final settlement;
do not stop timing when the last inference has merely been submitted.

The evidence is sufficient to start these experiments. Add only focused missing
measurements: qualification subphase timing/calls, admission batch-size and
reservation timing, per-image detected/embedded face counts, and container CPU
delta. A broad instrumentation-only round is unnecessary before benchmarking.

## Scope of the user-visible goal

This ranking targets Vision's 10k/hour goal. If the target is time until the
entire upload is published, indexing also deserves its own audit: the previously
verified index stage in this wave took about 5m44s, versus 8m08s in Vision and
1m48s in clustering. Faster Vision alone does not remove downstream publication
latency. Those stage durations are not a full end-to-end sum or concurrent
throughput calculation.

## Evidence files

- [Aggregate audit evidence](vision-optimization-priority-audit-2026-10-29.json).
- [Persistence/receipt experiments](../benchmarks/vision-persistence-azure-20261029.md).
- [Concurrency experiments](../benchmarks/vision-concurrency-azure-20261029.md).
- [Azure core/thread experiments](../benchmarks/vision-stages-azure-20261027.md).
- [Core batching experiments](../benchmarks/vision-core-batching-azure-20261027.md).
- [Scheduling and inspection suite](../benchmarks/vision-optimization-suite-20261028.md).

Older audit figures must be used with their workload/version and units attached.
In particular, the hours column in the earlier gap analysis is inconsistent with
its millisecond averages; it is not a valid cost-ranking input. This audit uses
raw milliseconds and the latest qualified source counts instead.
