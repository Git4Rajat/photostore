# Vision production timing investigation — 2026-10-27

The supplied averages reproduce in the deployed logs. CLIP and face latency is
mostly native execution, while applying results is a separate substantial cost.
The worker does not show sustained saturation of its two allocated CPU cores.
Prioritize result-application I/O and semantic text work before increasing CPU
or changing face/image models. This is a diagnosis, not a measured speedup.

## Evidence and coverage

- Deployment: `forenklaprep1-dev-vision--0000020`, image commit
  `2e0ad6c6b1b51ded9d44dbf2788eb57e3577ae97`.
- Latest measured wave: `39e91938bc3f4f7f85346f54246658e1`, 1,480 members,
  1,461 distinct source-processing image summaries and 1,461 apply calls.
  Normalize model/apply costs by those 1,461 calls, not all members.
- Thirteen stage starts match thirteen attempt summaries by attempt ID. One
  closing summary reports Vision complete; source errors and OCR timeouts are
  zero. All 1,461 metadata commits succeeded with no CAS conflicts or retry reads.
- Event-time envelope: 15:44:24–16:05:12 UTC, or **16:44:24–17:05:12 CET** in
  Stockholm, October 27. This approximately 20m48s envelope measures Vision
  attempts, not upload intake through final searchable publication.
- Read-only deployment inspection: latest revision equals latest ready revision,
  health Healthy, provisioning Succeeded. At inspection it was scaled to zero.
- Actual configuration: two vCPUs, 4 GiB memory, two source workers, shared
  decoding enabled, one OpenMP thread, min replicas zero and max replicas one.
  All 4,383 visual-stage input events identify preview bytes. Shared decoding
  and preview input are already active optimizations here.

See the adjacent JSON for aggregate evidence, and KQL for reproducible queries.
Raw query outputs and resource snapshots are under
`/tmp/photostore-vision-production`. No deployment, reprocessing, queue messages
or application data writes were performed during this investigation.

## Model stages

| Stage | Mean elapsed/image | P95 |
| --- | ---: | ---: |
| Face processing | 388.67 ms | 489.94 ms |
| CLIP image processor | 163.73 ms | 288.81 ms |
| OCR processor | 37.57 ms | 150.75 ms |
| Applying results | 602.61 ms | 754.96 ms |

Apply P95 comes from image-context counters. Model-stage P95 comes from individual
step events. These are overlapping/concurrent elapsed measurements, not an
exclusive decomposition of wave wall time.

Face diagnostics, averaged across all 1,461 source calls:

| Operation | Mean elapsed/image |
| --- | ---: |
| Decode | 1.46 ms |
| YOLO detection | 151.42 ms |
| Landmark lock wait | 0.03 ms |
| Landmark execution | 21.04 ms |
| Alignment | 0.41 ms |
| AdaFace embedding | 214.10 ms |

Detection and embedding account for about 94% of face elapsed time. Landmark
serialization and decoding do not explain the 389 ms. These are elapsed host
timings; native execution includes CPU scheduling, not just computation.

CLIP's 163.73 ms includes **140.69 ms forward inference**, **11.64 ms model-lock
wait**, **6.40 ms preprocessing**, and about 5 ms of other work. Median lock wait
is effectively zero; P95 is 75.15 ms, and 171/1,461 images wait more than 50 ms.
Forward inference is about 86% of the processor average. Even eliminating all
observed image lock wait would remove only 11.64 ms/image from this stage's
average, before considering effects on other work. It is not a measured
end-to-end saving.

OCR recognition averages 30.22 ms/image. Only **two engine initializations** occur,
with **1,459 worker reuses** and no timeouts or capacity failures. Initialization
totals 145.41 ms for the entire wave, about 0.10 ms/image. Engine reload overhead
has already been removed from the steady-state path.

## Applying/persisting results

The following peer phases and inclusive commit span account for about 584 ms of
the 603 ms average. The remaining approximately 19 ms covers work outside those
timers and their instrumentation. Commit child phases are not added again.

| Operation | Mean elapsed/image | Mean caller-thread CPU |
| --- | ---: | ---: |
| Face persistence | 127.85 ms | 32.49 ms |
| Semantic refresh | 120.27 ms | 82.65 ms |
| Derivative promotion | 101.02 ms | 32.26 ms |
| Artifact capture | 97.37 ms | 26.04 ms |
| Photo embedding write | 12.86 ms | 2.67 ms |
| Metadata commit, including completion delivery | 124.68 ms | not separately measured |

Face persistence, derivative promotion and artifact capture together average
**326.24 ms/image**. They are the largest combined non-semantic application path.
Their CPU/elapsed differences are consistent with waiting, but cannot establish
exclusive network latency: scheduling and native/helper work can also contribute.

Semantic refresh is real additional model work: deployed code builds semantic
text and calls the CLIP text encoder. Its 82.65 ms caller-thread CPU average is
evidence of substantial compute within the 120.27 ms phase. The remaining time
cannot be labelled purely as model-lock wait from this revision's data.

The 124.68 ms commit span includes lease/policy checks, row merge, completion
validation, metadata CAS, completion outbox delivery and index dirtiness. The CAS
write itself averages only **12.88 ms**. Completion delivery lacks its own timer
in this deployed revision, so the whole commit span is not one slow table write.

Across the entire wave attempt scope, transport tracing records **100,726 HTTP
calls**, or **68.94 per processed source**, with 997.98 seconds of accumulated
send latency, about 9.91 ms/call. These include inspection, qualification and
coordination outside apply, overlap across sources, and include expected
not-found/conflict responses in the generic error counter. They cannot be
subtracted from wall time or assigned entirely to persistence. Reducing serial
round trips is a concrete investigation target even when each request is fast.

## Resource and scheduling evidence

Azure Monitor one-minute buckets from 15:46–16:05 UTC, excluding the startup
minutes, report:

- Mean CPU usage **1.13 cores**, around **56%** of the two-vCPU allocation;
  maximum sampled usage 1.38 cores. This rules out sustained saturation in these
  samples, not short CPU bursts or slow single-thread performance.
- Mean working set **1.87 GiB**, maximum sampled **1.94 GiB**, within 4 GiB.
- Source-slot occupancy **92.32%** within the measured source-pool windows.
  Busy slots include I/O and waiting, not just CPU execution.
- Source executor queue delay averages **0.34 ms/source**. Continuation delays
  in the observed starts are roughly 0.12–0.16 seconds, not a large queue stall.

Thus, most admitted source slots stay occupied, but the CPU allocation is not
fully used continuously. Storage/application waits are a plausible contributor.
Simply assigning more vCPUs is not demonstrated to fix this; raising source
concurrency would need a controlled benchmark because it also increases model
contention. Only one replica is allowed, so the total fleet cannot scale beyond
this worker, but changing that trades throughput for cost and requires separate
measurement.

## Next measurement and instrumentation status

The local `d91e01d` commit already adds phase-context physical I/O counters,
per-table read/write buckets, semantic-text tokenization/forward/lock-wait
counters, semantic-vector reuse/batching, and a completion-delivery timer.
The deployed `2e0ad6c` image has none of those new fields. Existing phase
`sdk_calls` and `sdk_send_ms` use `sdk_attribution=shared_scope`, so concurrent
sources contaminate them as per-phase attribution; they were deliberately not
used to claim exact I/O time above.

The next instrumented upload should compare:

1. Phase-context request counts and read/write latency for face persistence,
   promotion, artifact capture and completion delivery.
2. Semantic text forward time versus lock/collection wait, reuse rate, real batch
   sizes, numerical output behavior and fallback count.
3. Apply median/P95, source-slot occupancy, CPU and total Vision envelope on a
   comparable fresh-source corpus. Separate duplicate receipt reuse from inference.

Do not infer a speedup from the newer unit-tested code before that upload. The
earlier local 48-image benchmark also uses a different image/face mix and macOS
runtime, so its 56 ms face and 25 ms CLIP averages are not production targets.
This investigation used the Azure diagnostics skill and only read deployed
state, logs and metrics.
