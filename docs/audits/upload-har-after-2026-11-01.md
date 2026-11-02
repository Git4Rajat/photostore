# Follow-up upload HAR — 2026-11-01

Source: the user's `download-af.devtools`, valid complete HAR JSON with 1,046
entries. This is an in-progress capture spanning **23:32:21–23:33:44 Stockholm
time** (22:32:21–22:33:44 UTC), about 83.08 seconds. The original capture covered
997.6 seconds. These are different observation windows, not a controlled
benchmark of complete selections.

The upload app's read-only resource inspection reports a succeeded deployment,
ready revision `forenklaprep1-dev-upload--0000052`, backend image commit
`8911bf990375bcaca56ede78bbfd8e3a974087f8`, 0.5 CPU / 1 GiB per replica,
HTTP scaling threshold four, and maximum five replicas. This confirms the
optimization commit is deployed; it does not establish replica health or count
throughout the captured interval.

## Completed requests only

| Measurement | Earlier HAR | Follow-up HAR |
| --- | ---: | ---: |
| Successful init requests | 692 | 39 |
| Successful finalize requests | 601 | 18 |
| Files per successful init batch | 1.75 | 2.03 |
| Files per successful finalize batch | 2.02 | 2.94 |
| Init median end-to-end | 903 ms | 1,116 ms |
| Init p95 end-to-end | 2,639 ms | 44,825 ms |
| Finalize median end-to-end | 6,885 ms | 26,473 ms |
| Finalize p95 end-to-end | 13,494 ms | 53,843 ms |
| Init median app duration (Server-Timing) | 835.5 ms | 703 ms |
| Finalize median app duration (Server-Timing) | 6,715 ms | 12,438 ms |
| Finalize median time outside app duration | 33 ms | 14,857 ms |
| Init storage calls/file, weighted | 37.0 | 25.2 |
| Finalize storage calls/file, weighted | 251.8 | 226.5 |

Request medians are not per-file costs: batches are larger in the new capture.
Storage calls/file are calculated as summed Server-Timing call counts divided
by summed result entries, using successful responses with timing headers.
These show approximately 32% fewer init storage calls/file and 10% fewer
finalize storage calls/file in the observed windows. They do not establish a
causal improvement under identical load. Percentiles use sorted durations
without interpolation; the new finalize sample has only 18 successes.

There are 45 init requests (39 HTTP 200 and six status zero), 29 finalize requests
(18 HTTP 200, one HTTP 503, ten status zero), and 45 registration requests
(44 HTTP 200 and one status zero). The 503 takes **59.23 seconds** and returns
`wave_admission_retry` / `Wave source admission requires retry`. The 18 successful
finalize responses contain 53 complete result entries, no per-item errors and no
duplicate lists, reporting 1.78 MB of source data. They do not prove the entire
selection finished or downstream work completed.

Status-zero API entries have zero recorded duration and no usable response;
treat them as uncompleted/unrecorded in this capture, not as instant successes
or proven server failures. Exclude them from successful-request percentiles.
Worker resource entries also use status zero and must not be treated as worker
crashes solely on that basis.

## Client improvements are visible

- **98 original image PUTs, no block PUTs or block-list commits.** The small-file
  single-PUT path is active. There are also 332 PUTs into the thumbnails
  container. All 430 PUTs return HTTP 201, median 61 ms and p95 115 ms.
- There are three raster-worker resource entries and 25 photo-metadata-worker
  entries, consistent with reduced worker creation. Capture boundaries and idle
  expiry mean these counts are not a complete worker-lifecycle inventory.
- Successful finalize batch size is about **46% larger**, consistent with the
  new collection window. The larger pending batches must not be mixed into that
  completed-request comparison.
- Captured finalize payloads report generated/uploaded preview, thumbnail and
  metadata assets. Median raster generation is 60 ms; metadata generation is
  41 ms. Preparation's median admission wait is 2 ms but p95 is 12,563 ms;
  total preparation median/p95 is 582/14,020 ms. These payload statistics include
  attempted/retried requests, not independent unique-file outcomes.

## Remaining bottleneck

End-to-end delays are dominated by waiting for the upload API, not blob PUTs.
For successful requests the median browser blocked time is 0.39 ms for init and
1.25 ms for finalize, while median time-to-first-byte is 1,114 and 26,468 ms.

Server-Timing exposes a large wait outside the instrumented Flask request:
one init takes 36.87 seconds end-to-end but only 813 ms in the app. A four-file
finalize spends 20.71 seconds inside the app with 867 storage calls and 16.54
seconds of attributed storage time. Attributed storage timings can overlap and
are not CPU timings. The server is still doing substantial storage work, while
other requests wait upstream of the request instrumentation. The HAR cannot
identify the exact upstream queue or prove CPU throttling.

The strongest next investigation is live-wave coordination: admission and
per-item source resolution repeatedly read and conditionally update shared
library policy/control, session and wave rows. Concurrent registration,
initialization and finalization can contend over these rows. The 503 is evidence
that the admission after-hook required retry; the HAR alone does not expose its
exception chain or prove which row conflicted.

Before another optimization or concurrency increase, correlate this interval's
outer request logs, storage-operation breakdowns, contention exceptions and
replica/CPU metrics. Likely work to evaluate is reducing per-file coordination
transactions and reserving request capacity for init/admission. More frontend
parallelism could worsen the observed queue. No application code or cloud
configuration is changed by this inspection.

## Correlated Azure evidence

Read-only Log Analytics query: **22:31:30–22:35:00 UTC**, deliberately wider
than the HAR. It returned 773 matching request/timing/contention/error records.
Counts below must not be interpreted as counts inside the 83-second HAR.

- 194 init requests: all HTTP 200, median outer app time 705 ms, median 46
  storage calls and 600 ms attributed storage time.
- 74 finalize requests: 69 HTTP 200 and five HTTP 503; median outer app time
  12.29 seconds, 662 storage calls and 9.45 seconds attributed storage time.
- Six `wave_mutation_contention` events: one `ShadowWaves.register.run`, five
  `ShadowWaves.resolve.run`. Each exhausted 16 attempts, spending 4.30–5.52
  seconds in that retry loop, with HTTP 412 and transaction index zero.
  Inspection of `_commit` shows the control-row change is transaction index
  zero. The finalize exception chain explicitly reports
  `UpdateConditionNotSatisfied` -> `MutationContention` -> admission retry.
- Inner finalize phase averages include client processing 5.51 seconds,
  persistence 5.07 seconds, finalize metadata 3.44 seconds, metadata packet
  1.16 seconds and metadata stamp 1.00 second. These phases nest; do not add
  them or interpret them as CPU measurements.
- Sample finalize operation breakdowns show jobs-table GETs, Table batch
  transactions and search-dirty-table GETs dominate. One sample has 1,075
  total storage calls: 483 jobs reads, 189 batch transactions and 183 dirty
  reads among its top operations. GET error counters include expected missing
  rows; they are not evidence of hundreds of failed uploads.

Code inspection identifies another important source of fan-out:
`automatic_index_updates.SourceTable` wraps metadata mutations with source
admission checks, target derivation, coverage begin/finish transactions and
coverage checks. Wave-owned source writes suppress scheduling but still run
coverage begin/finish. Repeated metadata writes for original finalization,
inline preparation and receipts therefore multiply the durable bookkeeping.
This is a concrete next optimization target, not permission to remove coverage
fences or defer durable intent until after source writes.

Azure Monitor, one-minute unsplit metrics (no replica breakdown requested):

| UTC minute | CPU average, cores | CPU maximum, cores | Memory maximum, MB | Replicas maximum |
| --- | ---: | ---: | ---: | ---: |
| 22:31 | 0.169 | 0.282 | 337.7 | 1 |
| 22:32 | 0.025 | 0.065 | 59.5 | 1 |
| 22:33 | 0.257 | 0.553 | 350.8 | 5 |
| 22:34 | 0.313 | 0.508 | 361.8 | 5 |

The 22:34 replica count is five for the full sampled minute; 22:33 averages
2.75. These metrics show scale-up during the capture and CPU peaks near the
0.5-core allocation, not proof of sustained fleet-wide CPU exhaustion. Unsplit
aggregations and minute resolution cannot identify each busy replica or prove
CPU throttling. Early warm-up/scale-up contributes to queueing; the continued
storage work and contention remain after scale-out.

Next work should target automatic-index bookkeeping per logical source update
and shared-control transaction contention, preserving pre-write durability,
cleanup fencing and retry safety. Validate the next change on a warmed service
with the same dataset and capture the complete selection. This inspection adds
only this audit document; no application code or Azure setting was changed.
