# Route to 10,000 Vision images/hour

Analysis only. No application code, deployment, permissions, or runtime configuration changed.

## Measurement basis

Wave `f54259517fe84c19864ec308b0682ad8`, 9,731 admitted members. The final run reached Vision readiness in approximately 132 minutes including interruptions and final qualification. This analysis uses 65 recorded productive passes containing at least 100 source attempts: 9,446 attempts. Interrupted/unreported work and final short passes are excluded from these averages. Production currently allocates 2 CPU, 4Gi RAM, two source workers, maximum one replica.

| Measurement | Value |
| --- | ---: |
| Productive-pass throughput | 5,166 images/hour |
| Best recorded approximately 100-second pass | 5,586 images/hour |
| Productive pass wall cost | 697 ms/image |
| Process CPU cost | 779 CPU-ms/image |
| Average process CPU consumption | 1.12 cores |
| Source worker busy duration | 1,090 ms/image |
| Model steps | 519 ms/image |
| Apply path | 510 ms/image |
| Source claim / release | 31 / 11 ms/image |
| Settlement | 69 ms/image |
| Member fetch / page loading / inspection prefetch | 1.2 / 9.4 / 2.7 ms/image |

Nested and concurrent durations are not additive elapsed times. Process CPU excludes child-process CPU and includes process-level overhead; use container/cgroup counters in the next experiment before treating this estimate as an absolute hardware bound.

10k/hour requires 360 ms elapsed/image. At unchanged CPU cost, 10k/hour needs approximately 2.16 process CPU cores before OCR children and headroom. Keeping two source workers requires reducing their mean busy duration from approximately 1,090 to below 720 ms/image, with additional room for coordinator work. Batching face writes alone cannot establish that reduction.

## Decisions

1. Benchmark a hybrid embedding-version read path first. The current multi-key OR filter is a partition scan under Azure Table semantics, not a batch of indexed point reads. Compare exact point reads with 1/2/4 read workers for 1/2/4/8/14 keys inside the Azure network. Compare complete face persistence, genuine ETags, missing rows, SDK pages/retries, median/p95 and requests/image. Retain bounded queries only where measured faster. The slow reads average roughly 11 ms over *all* images, so fixing them alone cannot double throughput.
2. Remove source-worker occupation by persistence. Prototype a bounded compute-to-apply handoff with two model workers initially, a separate small I/O pool, independent in-flight limits and byte limits. Keep source leases renewed through apply and qualification. Do not acknowledge a source or publish completion before durable commit. Errors and cancellation must drain/fence outstanding work. This differs from previously rejected concurrent model-stage queues.
3. Batch persistence across sources, emphasizing the common one-face case. Start with bounded groups of 8/16 images and a short deadline, flushing immediately for small uploads/end-of-wave. Batch lease/version selection where safe and same-table, same-partition writes with original ETags. Publish completeness per image last. Identify a conflicting image and rebuild the unaffected batch without refreshing/retrying the rejected user's curation write. Bound transaction count and body size. Validate actual SDK failure semantics before production enablement.
4. Benchmark 4 CPU/8Gi with 2/3/4 source workers using the existing schedule as a control, subject to environment support. Also test the decoupled pipeline at 2 CPU and 4 CPU. More workers on the same two-CPU compute benchmark previously regressed. Four cores are a capacity hypothesis, not a measured 10k guarantee. Preserve native single-thread limits while comparing.
5. Optimize actual CPU work if the target must fit two cores or capacity trials fall short. Prioritize face embeddings (~203 ms/image measured inference), CLIP image inference (~134 ms), and semantic text inference (~104 ms). Validate CPU-optimized same-model execution before model/precision changes. Ordinary ONNX graph optimization is already enabled by default; toggling it is not a novel gain. AdaFace's fixed singleton input needs a separately validated export for batching.
6. Revisit dense CLIP/semantic batches only after a deeper bounded pipeline can supply them. The existing two-source synchronous broker and independent model scheduling regressed in matched Azure tests; do not enable those experiments unchanged. Text result caching is valid only for identical canonical text and model/tokenizer version. Different image/text embeddings are not interchangeable.
7. Improve final qualification incrementally and ensure ownership cleanup/recovery on shutdown. Final reconciliation added approximately eight minutes in this run. This improves completion time, not inference peak. Preserve a final consistency check; never declare readiness from an unchecked cached count.

## Benchmark and rollout gates

Use identical representative inputs including single/multiple/no faces, no/dense/small text, originals and reusable previews. Include fresh output and existing user-curated rows. Measure 20-image and >=1,000-image runs; use longer sustained Azure trials for the final target. Alternate variant order, control CPU/RAM/thread settings and warmup, and include retries and partial failures in upload trials.

Report useful terminal images/hour, process plus child/container CPU per image, CPU throttling, model lock wait, queue wait, apply time, storage calls and service/tail latency, batch occupancy, memory high-water marks, and time from last inference to Vision readiness. Keep model quality/identity gates and stale-source/curation protection mandatory. Excluded/failed/superseded members are not successful inference throughput.

Suggested matrix: current 2 CPU/two workers; decoupled 2 CPU/two compute workers plus I/O pool; decoupled plus cross-image persistence batches; unchanged model schedule at 4 CPU with 2/3/4 workers; winning persistence configuration at 4 CPU. Advance only improvements demonstrated in complete matched runs. Do not multiply isolated benchmark percentages to predict whole-wave gains.

Success means >=10,000 successfully processed images/hour over a sustained, representative interval, without a growing apply queue or quality regressions. Report whole-wave elapsed time separately. For 9,731 images, steady 10k/hour corresponds to 58m23s of processing before startup, interruptions and completion-tail overhead.

## References

- [Matched Azure core/thread tests](vision-stages-azure-20261027.md)
- [Azure CLIP/YOLO batching tests](vision-core-batching-azure-20261027.md)
- [Scheduling and storage suite](vision-optimization-suite-20261028.md)
- [Microsoft Table query design](https://learn.microsoft.com/en-us/azure/storage/tables/table-storage-design-for-query)
- [ONNX graph optimizations](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html)
- [ONNX quantization tradeoffs](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
