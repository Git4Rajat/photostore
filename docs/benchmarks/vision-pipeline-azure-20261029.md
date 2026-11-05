# Vision qualification and compute/apply benchmarks — 2026-10-29

**Implement fresh post-source qualification batching first. Defer the separate
compute/apply pool.** Qualification itself becomes much faster, but the matched
real-model replay improves by approximately 7%, not 74%. Handoff improvements
are smaller and do not meet the predefined follow-up threshold.

## Setup and scope

- Configured worker image `32b9e9b7e014e1eeb8204ceff6536a0ffad94006`, disposable
  West Europe Container Apps job, 2 CPU / 4Gi. Observed cgroup quota:
  `200000 100000`. OpenBLAS and OpenMP limits remain one.
- No managed identity, production credentials, production mounts or queue
  consumers. Six tables in a private temporary storage account.
- **78 measured comparisons**, three alternating-order rounds per scenario.
  **11,880 standalone source-qualification checks** and **1,896 real-model image
  passes**. Fixture creation, model/processor warmup and durable output readback
  are excluded from timings.
- Qualification fixtures have valid source contracts, immutable face snapshots,
  manifests and accepted completion inboxes. They are synthetic, not copied
  production photos. The large-partition control adds 10,000 background rows
  each to the library metadata and coordination partitions; the immutable
  artifact partition is not similarly padded.
- Real-model inputs are the existing 48-entry corpus (45 distinct preview byte
  sequences). The face-positive profile is 15 inputs repeated four times per
  comparison; the small profile uses the first 20 inputs. The same processors,
  weights, frames and output checks are used across variants. Semantic encoding
  is actual inference, not sleep or a synthetic stand-in.

The pipeline replay includes real source-attempt admission transactions and
policy guards, claims, OCR/face/image CLIP, semantic refresh, face persistence,
artifact capture, metadata CAS, completion acceptance/ACK, source release and
post-source qualification. It ends after all apply and qualification work drains.

It excludes blob/derivative handling and transport, actual stage ownership checks,
whole-wave member observation commits/freezing, queue dispatch and downstream
publication. The source rows and member descriptors are isolated fixtures, not a
complete replay of upload intake. Consequently this is a **matched pipeline
component experiment**, not a production throughput measurement or proof of
10k/hour. Do not multiply its percentages by prior isolated benchmark gains.

## Qualification experiment

Compare the existing serial path with four parallel source qualifications and
fresh batched evidence reads in independent 20-source groups. Each parallel
source has its own receipt cache; it does not share mutable cache internals.

The batch mode fetches a new post-processing metadata/evidence batch. It then
retains **one fresh metadata point read per image** when qualifying that source.
That final read prevents a replacement or newly acquired lease after prefetch
from being authorized against the old batch snapshot. Immutable receipt hashes,
source bindings and model-contract checks are unchanged. Pre-processing absence
observations are not reused.

The first prototype lacked this post-prefetch safeguard. It was stopped and
superseded; none of its timings are included below. Local race tests cover the
corrected behavior.

| Workload | Serial median | Parallel median | Batch median | Batch paired wall reduction |
| --- | ---: | ---: | ---: | ---: |
| 1,000 sources, one face each | 47.283s | 15.604s | **12.073s** | **73.8%** |
| 200 sources, one face, populated partitions | 9.545s | 3.101s | **2.533s** | **73.6%** |
| 20 sources, zero faces | 0.742s | **0.223s** | 0.247s | 69.2% |
| 100 sources, eight faces each | 9.036s | 3.118s | **1.461s** | **83.8%** |

Percent changes are medians of candidate/control ratios paired within each
round, not ratios of the displayed median durations. The 1,000-source batch
reductions range from 68.1% to 74.6%; the populated-partition reductions range
from 73.5% to 73.9%.

For 1,000 one-face sources, serial and parallel modes each make **7,000 SDK
method calls**. Batch mode makes **1,600**, including all 1,000 fresh source reads:
77.1% fewer calls. For the eight-face control, the count drops from 1,400 to 210.
`SDK calls` counts outer table methods, not internal HTTP retries or continuation
pages. All pagination/SDK execution time remains inside elapsed timings.

The small-upload parallel mode is slightly faster than batching by about 24ms
at the displayed medians. That small difference does not establish a special
threshold policy; both improve substantially over the serial control.

## Compute/apply handoff

Control: three whole-source workers, each doing compute and apply synchronously.
Candidates: two compute workers with one, two or three apply workers. Admission
happens on the scheduler before work starts. The handoff bounds in-flight sources
to `2 + apply_workers`, reserves a 32MiB output budget, and drains active work
before returning or propagating an error. Result association and source fences
are retained; a source is counted complete only after apply succeeds.

| Configuration | Mixed paired wall reduction | Face-positive | Small, 20 images |
| --- | ---: | ---: | ---: |
| Two compute + one apply | 0.7% | 2.0% | 1.6% |
| Two compute + two apply | 0.4% | 3.2% | -1.4% |
| Two compute + three apply | **3.5%** | **4.0%** | 0.8% |

Negative means slower. The two-compute/three-apply mixed results range from
**4.0% slower to 7.2% faster**. Its face-positive results range from 1.8% to 8.5%
faster. The control face-positive median is 34.037s for 60 images, versus 32.428s
for this candidate, a 4.73% ratio-of-medians reduction.

The follow-up gate was at least 5% lower face-heavy median elapsed time. It was
not met, so the optional combined handoff follow-up was not run. This is a
prioritization decision, not evidence that handoff can never help another
workload or resource allocation.

The replay's CPU counters explain why adding I/O workers is not a large gain:
the face-positive control averages approximately 1.63 container cores, rising to
1.71 with three apply workers. Its measured CPU work is roughly 0.9 CPU-seconds
per image. Apply includes semantic inference and client-side serialization, not
only network waits. The code's OCR workers are resident **threads**, so their
CPU is already included in process CPU; process and cgroup totals are close.

For this corpus, roughly 0.9 CPU-seconds/image would require about 2.5 CPU cores
at 10k/hour, before additional application work. Pure I/O overlap cannot remove
that CPU requirement. This is not the same workload/cost as the latest live
upload's approximately 0.6 CPU-seconds/image; absolute rates must not be
transferred between them.

## Qualification inside the real-model replay

An independent three-round matched comparison keeps the existing three-source
schedule and changes only serial versus fresh-batch qualification. This is the
most useful evidence for the next production change.

| Configuration, 60 face-positive image passes | Median elapsed | Median SDK calls |
| --- | ---: | ---: |
| Current schedule + serial qualification | 33.707s | 2,605 |
| Current schedule + fresh-batch qualification | **31.505s** | **2,276** |

The **paired median wall reduction is 7.1%**, ranging from **4.8% to 9.6%**.
The median qualification interval itself drops from 3.750s to 1.022s. Thus the
large qualification-only reduction is diluted by unchanged model and apply work,
as expected. This does not establish a 7.1% production-upload improvement.

## Correctness, resources and limits

- All per-input model/semantic fingerprints and per-profile canonical output
  digests match. The independent integrated profile matches the earlier
  face-positive output digest too. Face membership and vectors are checked by
  fresh durable reads outside timing.
- Independent table readback verified **3,216 durable source records, 3,476 face
  snapshots and all 1,896 real-model completion ACKs**, with valid manifest and
  snapshot hashes and bound completion inboxes. Qualification fixtures account
  for the remaining 1,320 source records; background/superseded fixtures are not
  counted as measured source completions.
- **113 local tests pass**: prototype tests plus existing receipt and wave-batch
  tests. Cases include post-prefetch replacement/lease acquisition, stale-source
  rejection, missing/corrupt evidence, avoiding stale pre-source absence,
  positive/zero-face apply/capture/ACK, bounded queues, output-size limits and
  draining active apply work on failure. Fault injection uses ETag-aware local
  tables; the Azure runs are successful-path SDK/durability tests.
- Every measured handoff group drains to zero pending results and stays within
  its count/serialized-output byte bounds. That byte metric excludes native model
  memory, decoded images and the preloaded corpus; it is not a total RAM limit.
  Process peak RSS is a lifetime high-water mark, not per-variant memory.
- Source leases complete within their normal lifetime in this replay. Transfer
  of production heartbeat/context lifetimes and stage ownership is not validated
  by this handoff prototype. No production enablement is recommended from it.
- Three rounds and a small repeated model corpus limit confidence and workload
  generalization. Image/face/OCR processors are warmed; the first semantic-text
  call is in the measured sequence. Source-level p95 distributions were not
  retained by the measured runner; elapsed time, CPU, durable outcomes and queue
  bounds are the primary results. Whole-wave startup/tail latency is excluded.

## Decision

1. Code fresh post-source evidence prefetch with the retained fresh metadata read
   and a point-read fallback when prefetch fails. Preserve the current ownership,
   cleanup, deletion/replacement and final-freeze checks. Instrument qualification
   fetch/verification/fallback wall time and SDK calls.
2. Validate the change on an actual fresh upload before claiming a production
   gain. It is a useful reduction in serial overhead, not the whole route from
   7.4k to 10k/hour.
3. Defer the compute/apply split. The next larger experiment should reduce real
   face/CLIP/text CPU cost, or use a controlled four-CPU comparison to distinguish
   runtime efficiency from capacity, rather than adding more scheduling machinery.

No production application, queue, permissions or configuration was changed.
Temporary-resource deletion and absence verification are recorded in the raw
report after cleanup.

Raw results include the **exact measured runner snapshot**, hash and helper
sources: [vision-pipeline-azure-20261029.json](vision-pipeline-azure-20261029.json).
The current reusable runner additionally supports per-source latency collection
and an optional combined follow-up; those additions were locally tested but were
not used for these Azure measurements.

Runner: `backend/scripts/benchmark_vision_pipeline.py`.
Tests: `backend/tests/test_benchmark_vision_pipeline.py`.
