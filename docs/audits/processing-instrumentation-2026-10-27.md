# Watching the processing optimizations

Counters aggregate in memory and emit summaries, not a log for each SDK call.
No telemetry-specific storage operation is added. Dispatch timing fields are
piggybacked on the existing release/claim writes. Identifiers in image logs are
hashed; paths, source filenames, tokens and payloads are never counter labels.

## Events and attribution

- `vision_image_summary`: one source-member/group pass (hashed `member_id`), combining its inspection,
  processing and qualification contexts. Concurrent images have separate counters;
  child executor work and source heartbeat I/O inherit their owner's counter.
  `source_attempts=0` distinguishes receipt replay from a source-processing call.
  `receipt_qualified` is proof observed by this pass, not a claim that the later
  wave observation transaction committed. `outcome` describes group execution.
- `vision_wave_attempt_summary`: whole coordinator attempt, including shared
  work outside image contexts. `measurement_scope=attempt`, `attempt_id`,
  `dispatch_attempt`, `wave_id`, final returned state, `vision_complete`, member
  count and failed-member count (when supplied by the authoritative result).
  `process_cpu_ms` is process CPU, not image processing elapsed time.
- `cluster_wave_summary`: same storage/batch/artifact/journal totals for a
  clustering attempt. `cluster_wave_phase` includes counters for that inclusive
  phase. Nested phase totals overlap: never sum them to obtain wall time.
- `wave_retry_scheduled` / `wave_stage_started`: join by `schedule_id` to compare
  requested delay with `actual_delay_seconds` at the next stage start;
  `excess_delay_seconds` includes queue polling, cold start and coordination.
  Reasons distinguish productive continuation, barrier wait and coordination
  conflict. `attempt_id` ties the stage start to its attempt summary. Timing is
  consumed once and ignored on a newer request revision. The measurement ends
  at coordinator stage start, not the first model invocation.

An attempt summary contains:

- Physical transport requests: `storage_calls`, overlapping `storage_send_ms`,
  `storage_bytes`, `storage_errors`, `metadata_point_reads`,
  `metadata_query_requests`, `metadata_read_ms`, `job_read_requests`, `blob_gets`,
  `blob_heads`. HTTP retries count as separate requests. Non-2xx errors include
  expected missing-row/create-conflict responses; they are not failed images.
- Metadata reads by purpose: `metadata_claim_reads`, `metadata_heartbeat_reads`,
  `metadata_artifact_write_reads`, `metadata_derivative_promotion_reads`,
  `metadata_artifact_capture_reads`, `metadata_metadata_commit_reads`,
  `metadata_metadata_cas_reads`, `metadata_lease_and_policy_check_reads`,
  `metadata_source_release_reads`, `metadata_inspection_reads`,
  `metadata_qualification_reads`, other bounded phase labels and
  `metadata_other_reads`. These count point and query requests; their sum equals
  `metadata_point_reads + metadata_query_requests`.
- Logical batches: `batch_requests`, `batch_queries`, `batch_keys`, `batch_rows`,
  `batch_missing`, `batch_point_reads` (compatibility fallback). Logical query
  calls differ from transport requests when paging/retries occur. Receipt-cache
  hit/miss/eviction counts are separate.
- Artifact operations: `artifact_gets`, `artifact_heads`,
  `artifact_verifications`, `artifact_verified_bytes`,
  `artifact_verification_reuse`, `artifact_create_conflicts`, `receipt_creates`,
  `receipt_create_conflicts`. Logical attempts remain available with fake storage
  or when SDK transport tracing is disabled.
- Mounted journal: `journal_flushes`, `journal_flush_ms`, `journal_flush_errors`,
  `journal_fsyncs`, `journal_fsync_ms`, `journal_fsync_errors`. Flush elapsed
  includes buffer flush, fsync and local SQLite commit; fsync time is nested.
- `image_processing_ms` sums source-adapter elapsed (models and persistence),
  `image_inspection_ms` and `image_qualification_ms` describe the other image
  segments. Parallel image durations and SDK durations must not be subtracted
  from wall time. Shared coordinator operations stay in the wave attempt total.

`storage_tracing_enabled=false` means physical request counters are unavailable,
not proof of zero I/O. Existing shared-scope SDK fields remain for compatibility;
use the new image-context counters for per-image attribution.

A complete wave spans deliveries and may span process restarts. Its view is
aggregated from attempt events in Log Analytics, rather than writing extra
counter rows to Table Storage. A killed attempt can lack its closing summary;
compare stage-start/summary `attempt_id`s and treat missing summaries as incomplete
measurements. Do not manufacture cumulative totals from process-local state.

## KQL: Vision wave comparison

Use the same preamble for each query below, setting the desired wave ID.

```kusto
let targetWave = "82a439bce7d145ed977579f791c7ca12";
let events = ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h)
| where Log_s contains "processing telemetry"
| extend d = parse_json(extract(@"processing telemetry (.*)$", 1, Log_s))
| where tostring(d.wave_id) == targetWave
| extend at = todatetime(d.at);
events
| where tostring(d.event) == "vision_wave_attempt_summary"
| summarize
    first = min(at - tolong(d.milliseconds) * 1ms), last = max(at),
    attempt_elapsed_ms = sum(todouble(d.milliseconds)),
    process_cpu_ms = sum(todouble(d.process_cpu_ms)),
    metadata_point_reads = sum(tolong(d.metadata_point_reads)),
    metadata_queries = sum(tolong(d.metadata_query_requests)),
    storage_calls = sum(tolong(d.storage_calls)),
    members = max(tolong(d.member_count)),
    failed_members = max(tolong(d.failed_members)),
    completion_events = countif(tobool(d.vision_complete)),
    summaries = dcount(tostring(d.attempt_id))
| extend wall_seconds = datetime_diff('millisecond', last, first) / 1000.0,
         aggregate_attempt_seconds = attempt_elapsed_ms / 1000.0,
         point_reads_per_member = metadata_point_reads / todouble(members)
```

Wall time is the observed attempt envelope and excludes upload intake and time
before the first attempt. Aggregate attempt elapsed is separate; overlapping or
missing attempts prevent it being an exact wall-time decomposition. To audit
coverage, replace the final query with:

```kusto
events
| where tostring(d.event) in ("wave_stage_started", "vision_wave_attempt_summary")
| where tostring(d.stage) == "vision" or tostring(d.event) == "vision_wave_attempt_summary"
| summarize starts=countif(tostring(d.event)=="wave_stage_started"),
            summaries=countif(tostring(d.event)=="vision_wave_attempt_summary")
  by attempt_id=tostring(d.attempt_id)
| where starts != summaries
```

For read-count distribution, aggregate repeated passes by image first:

```kusto
events
| where tostring(d.event) == "vision_image_summary"
| summarize reads=sum(tolong(d.metadata_point_reads)), passes=count(),
            source_attempts=sum(tolong(d.source_attempts)) by member_id=tostring(d.member_id)
| summarize images=count(), mean_reads=avg(reads),
            percentiles(reads, 50, 95, 99), max_reads=max(reads),
            total_source_attempts=sum(source_attempts)
```

Image totals exclude shared coordinator queries and group admission/observation
writes. Compare the previous ~22 point reads/image against the whole-attempt
`point_reads_per_member`, then use image purpose counts to find remaining costs.

## KQL: retry delays and clustering overhead

With the same preamble, use:

```kusto
events
| where tostring(d.event) == "wave_stage_started" and isnotempty(tostring(d.schedule_id))
| project at, stage=tostring(d.stage), reason=tostring(d.reason),
          schedule_id=tostring(d.schedule_id),
          requested_seconds=todouble(d.requested_delay_seconds),
          actual_seconds=todouble(d.actual_delay_seconds),
          excess_seconds=todouble(d.excess_delay_seconds)
| order by at asc
```

An unmatched scheduled wake is visible with:

```kusto
events
| where tostring(d.event)=="wave_retry_scheduled"
| extend schedule_id=tostring(d.schedule_id)
| join kind=leftanti (
    events | where tostring(d.event)=="wave_stage_started"
    | project schedule_id=tostring(d.schedule_id)
  ) on schedule_id
```

For clustering, inspect the latest successful attempt (or filter `attempt_id`
explicitly when comparing retries):

```kusto
events
| where tostring(d.event)=="cluster_wave_phase"
| project at, attempt_id=tostring(d.attempt_id), phase=tostring(d.phase),
          seconds=todouble(d.milliseconds)/1000.0,
          storage_calls=tolong(d.storage_calls),
          batch_queries=tolong(d.batch_queries), batch_keys=tolong(d.batch_keys),
          batch_rows=tolong(d.batch_rows),
          flushes=tolong(d.journal_flushes), fsync_seconds=todouble(d.journal_fsync_ms)/1000.0
| order by at desc, seconds desc
```

The telemetry changes are ready for deployment testing; these example queries
have not yet been run against events from the new image. No live deployment or
queue operation was performed while adding instrumentation.

Validation: 1,177 regression tests passed. The final correlation, heartbeat and
source-member attribution adjustments also passed 136 lease/metrics/runtime
checks and 74 source/consumer/diagnostics checks. The overlap test intentionally
uses two distinct source members with the same filename: their counters and
qualification flags remain separate. git diff --check passed.

## Rolling Vision source pool

Vision now admits a rolling, bounded set of source adapters within each 20-member
page. A free slot is refilled without waiting for its slower sibling. Admission
transactions contain only the newly available slots, so there may be more small
admission transactions than with fixed pairs; `vision_admission_batches` exposes
that tradeoff. Member observations still commit together after the page's admitted
sources drain. On deadline, the unvisited tail consumes no attempts and the
checkpoint covers only the inspected prefix. Exceptions also drain submitted work
before releasing stage ownership, including with a shared executor.

New fields in image/wave counters:

- `vision_source_queue_ms`: submission-to-start delay, including contention for
  the shared executor. It is outside `vision_source_busy_ms`.
- `vision_source_busy_ms`: elapsed inside executing source adapters, including
  models, persistence and lease operations. It is not model-only CPU time.
- `vision_pool_ms`: coordinator admission/execution/drain window, excluding the
  subsequent qualification phase; `vision_pool_capacity_ms` multiplies this by
  configured source slots. Their ratio with summed busy time measures source
  slot occupancy, not CPU utilization. Shared executors can reduce occupancy.
- `vision_coordinator_wait_ms`: coordinator waiting for at least one future;
  this overlaps source execution and is not wasted time to subtract from wall.
- `vision_admission_ms`: inspection, reservation and submission elapsed. In the
  inline single-worker path it also includes source execution.
- `vision_settlement_ms`: authoritative boundary check and receipt qualification
  after draining, excluding the later page observation transaction.
- `vision_source_{claim,steps,apply,qualify,release}_ms`, corresponding `_calls`
  counts and `_errors`: source adapter boundaries. `steps` includes lazy
  input acquisition; model-step events exclude that acquisition. These nested
  durations must not be added to source busy time.
- `face_{decode,detect,landmark_wait,landmark,align,embed}_ms`: existing native
  face diagnostics now reach image/wave counters, including the wave adapter.
  Landmark execution excludes its separately measured lock wait.
- `ocr_engine_inits`, `ocr_engine_init_ms`, `ocr_recognition_ms`: initialization
  and native recognition separately. Accounting is carried into the OCR thread
  without copying execution fences. A timed-out native call may finish after
  its image summary and must not be interpreted as a complete timing sample.
- `clip_preprocess_ms`, `clip_lock_wait_ms`, `clip_inference_ms`: preprocessing,
  model lock acquisition and forward call separately. Forward elapsed is host
  elapsed; asynchronous GPU execution would require device-specific measurement.

`image_inspections` marks visited members. Deadline-skipped members no longer
produce empty image summaries. The SDK `photometadata()` query endpoint now
counts as `metadata_query_requests`; earlier builds incorrectly counted it as a
point read. Combined metadata GET totals remain comparable across builds.

Example (use attempt summaries only to avoid double-counting image summaries):

```kusto
ContainerAppConsoleLogs_CL
| where Log_s contains "processing telemetry "
| extend d=parse_json(extract("processing telemetry (.*)$", 1, Log_s))
| where tostring(d.event) == "vision_wave_attempt_summary"
| summarize wall_ms=sum(todouble(d.milliseconds)),
    busy_ms=sum(todouble(d.vision_source_busy_ms)),
    capacity_ms=sum(todouble(d.vision_pool_capacity_ms)),
    queue_ms=sum(todouble(d.vision_source_queue_ms)),
    admission_ms=sum(todouble(d.vision_admission_ms)),
    settlement_ms=sum(todouble(d.vision_settlement_ms)),
    claim_ms=sum(todouble(d.vision_source_claim_ms)),
    steps_ms=sum(todouble(d.vision_source_steps_ms)),
    apply_ms=sum(todouble(d.vision_source_apply_ms)),
    ocr_init_ms=sum(todouble(d.ocr_engine_init_ms)),
    clip_wait_ms=sum(todouble(d.clip_lock_wait_ms))
  by wave_id=tostring(d.wave_id)
| extend source_slot_occupancy=busy_ms / capacity_ms
```

The summed attempt `wall_ms` excludes inter-attempt gaps and any killed attempt
that never emitted its closing summary. Use stage-start events and durable freeze
time for end-to-end wall time, and identify missing summaries by attempt ID.

## Inspection/persistence/OCR follow-up

See [Vision follow-up](vision-overhead-followup-2026-10-27.md) for the bounded
prefetch and reusable OCR-worker changes. New attempt/image counters include
`vision_inspection_prefetches`, `vision_inspection_prefetch_ms`,
`vision_inspection_prefetch_errors`, `receipt_write_batches`, `ocr_worker_starts`,
`ocr_worker_reuses`, `ocr_worker_timeouts`, and `ocr_worker_capacity_errors`.
Prefetch duration moved out of per-member inspection; include both when comparing
inspection cost across versions. A resident OCR worker receives each request's
accounting context anew and clears it after that request. Timed-out workers are
retired and cannot contribute a complete per-image native timing sample.
