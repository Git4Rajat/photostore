# Index maintainer performance analysis — 2026-11-06

The largest measured optimization opportunity is the per-photo metadata preparation path, followed by Search publication. Current code already contains important improvements that are too recent to have a measured wave completion in this sample. Benchmark those changes before increasing compute or implementing another batching layer.

## Scope and evidence

Read-only Azure inspection of `forenklaprep1-dev-indexer`, resource group `forenklaprep1-dev-rg`, plus current source review. Log queries used rolling 48-hour windows during the morning of November 6, Stockholm time; resource metrics cover November 5 08:45 through November 6 08:45 Stockholm time. Windows differ slightly because collection was sequential. No queues were consumed, application endpoints invoked, data repaired, deployments changed, or production load generated.

Raw query results and saved KQL are in ignored local directory `.azure/index-maintainer-performance-20261106/`. `telemetry.json` and `parsed-telemetry.json` contain attempt/phase records; `projection.json` contains metadata projection aggregates; `outcomes.json` contains maintenance/recovery aggregates; `http-memory.json`, `metrics.json`, `config.json` and `system.json` provide corroborating evidence.

At inspection, provisioning succeeded and latest/ready revision was `0000017`, image `2d49fe9d577b0b0a32773ed915b487f0d68941f2`, matching the checkout. The Tools API reserves 0.5 vCPU / 1 GiB and maintenance reserves 1.5 vCPU / 3 GiB. Both containers scale together, minimum zero, maximum one replica, polling 30 seconds, cooldown 60 seconds. The primary queue counts all messages; the deferred queue counts visible messages.

**Version limitation:** the 38 measured index attempts span 21 wave IDs and multiple deployed revisions. They are historical workload observations, not a benchmark of `0000017`. That current revision had no `index_wave_summary` in the collected telemetry. Its three measured recovery passes totaled 3.74 seconds, maximum 1.43 seconds.

## Measured indexing work

Across those attempts: 3,035 seconds of summed attempt wall time, 912 seconds of process CPU, 251,300 storage calls, and 143,210 job-table read requests. All 38 attempts reported success; some returned `state=indexing` after a bounded slice rather than completing a wave. No storage throttling or storage server errors were recorded in these summaries. The 33,353 not-found responses are counted separately; do not equate these with failed attempts.

Two useful examples show where time goes:

| Measurement | Wave `98d5…`, revision `0000009` | Wave `0c736…`, revision `0000011` |
|---|---:|---:|
| Attempt wall time, summed per wave | 299.06 s across 3 slices | 91.82 s across 1 slice |
| Process CPU | 76.08 s | 71.93 s |
| Storage calls | 22,642 | 12,266 |
| Job-table reads | 11,490 | 2,177 |
| Prepare phase | 146.47 s / 12,210 calls | 42.14 s / 10,843 calls |
| All publication phases | 39.44 s / 329 calls | 38.50 s / 334 calls |
| Search publication | 15.22 s | 18.03 s |
| Eligibility checks, inclusive | 50.48 s / 5,152 calls | 4.47 s / 424 calls |

The first wave is the previously recorded 1,000-photo workload. Projection logs for revision `0000011` contain 499 completed projections. Sizes, versions, and concurrency differ: **the 299-to-92-second difference is not a controlled speedup measurement**.

Phase times are inclusive. Guard reads are nested inside eligibility checks, which can themselves occur inside prepare/publication. Do not sum all phase rows, or subtract nested guard time as if it were independent work. Concurrent SDK send durations can exceed elapsed time: the later wave reports 171.06 seconds of storage send time within 91.82 seconds of wall time. Process CPU also includes other threads in the same process.

## Findings and recommended order

### 1. Preparation still generates many storage round trips

Preparation accounts for 49% and 46% of attempt wall time in the examples, and 54% and 88% of their storage calls respectively. `wave_runtime.py:392` projects each settled/failed-exhausted member through `app._live_faiss_metadata_update` (`app.py:6206`). That function reads filename-to-face membership, face rows and person labels, then updates photo metadata and emits coverage/dirty completion state.

In revision `0000011`, projection timing sums were 266.11 seconds in the write/completion segment versus 15.60 seconds in face lookup/retrieval and 9.48 seconds in person lookup. These are overlapping per-photo service times, not additive stage wall time, but they identify metadata writes and follow-up bookkeeping as the first path to profile. Median projection latency was 516 ms and p95 1,427 ms under that revision's concurrent workload.

Already implemented:

- `84abc5a` introduced concurrent preparation and page-batched publication markers.
- `d7ea1c9` subsequently shared source reservations across each preparation page and set preparation concurrency to four.
- Current `prepare_page` uses `coalesced_source_reservations()` and context-preserving `map_members`. Current `CoalescedCheck` shares authoritative checks while retaining local lease checks and forced boundary checks.

The last measured wave predates `d7ea1c9`. First measure the current code using the same library and input wave size. Then consider page-level prefetch of face/person rows, reuse of label lookups within the page, and batching compatible metadata/dirty writes. Keep per-row ETag checks, durable source reservations, and publication fences. Increasing thread counts alone may increase contention; increasing replica count also changes publisher safety and is not a substitute for reducing per-photo calls.

### 2. Search publication is the largest remaining publication phase

Search takes 15–18 seconds in the examples, compared with roughly 6–7 seconds each for Sort and People Embedding. Across the historical sample, Search used 157.03 seconds of inclusive phase wall time and 107.92 seconds of thread CPU. This is a more CPU-heavy phase than the main preparation path.

`wave_catalog.py:145` runs publication scopes sequentially and invokes the Search build hook. Sort, Access, and People use full rebuilds in this wave adapter (`force_full=True`); Search also takes its build-hook path. Consequently some wave cost can grow with total library size, even when the new wave is small. Full scans and immutable output copies remain relevant at large-library scale.

Instrument Search scan, SQLite construction, compression, upload, immutable copying, and verification separately before choosing its next optimization. Benchmark a fixed new-photo count against growing existing libraries. The current sample cannot establish 100k/1M-photo throughput or memory requirements. Automatic photo-vector builds are already disabled by default; recommending that removal again would duplicate an existing change.

### 3. Background recovery deserves monitoring, with a strong version caveat

The collected outcome query recorded 297 recovery passes totaling 1,695 seconds, compared with 4,038 seconds across 439 successful message-handler phases. Thus recovery accounts for about 30% of these two categories combined. Handler success means the message handler returned normally; it does not prove index publication. Separate outcome records include completed, deferred, retry, obsolete and done results.

This aggregate is heavily influenced by older revisions: some recovery passes took 65–68 seconds. Latest revision `0000017` measured only 1.07–1.43 seconds per pass. Do not apply the historical 28-minute recovery total as a current-regression claim.

`run_index_worker` (`app.py:16730`) synchronously runs recovery at startup and every 60 seconds before polling work. `recover_pending_index_work` (`app.py:11204`) queries coverage controls with `RowKey eq '!coverage'` across library partitions, scans durable pending work, and nominates up to 25 libraries for cleanup per sweep. Cleanup candidate count is bounded; coverage discovery is still fleet-wide. This can grow with the number of libraries and can delay queued work when a sweep is slow.

Measure recovery query pages, storage calls, cleanup time and queue delay independently. If these grow materially, preserve durable recovery but separate its cadence from artifact GC, remember safe next-eligible cleanup times, and use bounded discovery. A deferred cleanup inside reader grace is expected; do not bypass grace or clear source reservations to reduce latency. The sample logged 528 cleanup reports, including recurring hash timestamp-evidence deferrals.

### 4. API requests and maintenance share allocation and latency boundaries

There were 704 successful `/api/tools/indexes/status` requests: p50 168.7 ms, p95 279.7 ms, maximum 3,369.2 ms. All returned 200. These are server-side request timings; they do not measure complete browser-perceived cold-start latency. API memory samples peaked at 307 MiB; maintenance samples peaked at 1,186 MiB. Sparse samples do not establish safe smaller memory limits.

Azure's app-level CPU metric peaked at about 1.77 cores in the 24-hour metric window. The mean replica count over five-minute buckets was 0.139, approximately 3.34 replica-hours integrated over the day. This is a runtime estimate, not an invoice quantity. CPU and working-set metrics use Replica/Revision dimensions, not per-process allocation attribution ([Azure metric definitions](https://learn.microsoft.com/azure/container-apps/metrics)).

Both containers start for HTTP or queue demand. Independent API/worker deployment is a potential cost isolation improvement, but savings cannot be inferred while triggers overlap. The single serial queue worker can also place small maintenance jobs behind long wave slices. Current wave slices default to 120 seconds and yield at durable boundaries, so this is not a hard 120-second request-wait bound. Measure enqueue-to-start latency by scope/library before adding concurrency or changing allocation.

### 5. The old runaway retry diagnosis should not be reused as the current conclusion

Current code enforces deferred work admission, parks active-wave work as `wave_wait`, and resumes it from wave completion (`app.py:11395`, `app.py:11413`). The current collection does not reproduce the November 2 thousands-of-attempts-per-hour barrier storm. Many `SourceBusy` mentions in this sample occur inside normal cleanup-deferral reports, so text-match totals are not retry counts.

Historical system-log queue scaler 403 failures last occurred November 4 around 11:33 Stockholm time, not at the current inspection. They should not be presented as evidence of a current permissions incident. No maintenance polling failure lines were counted in the collected activity query.

## Validation to run next

Use a repeatable 1,000-photo workload on the current image, with the same existing-library size and face/person distribution. Capture per-wave preparation, Search, marker finalization, source-reservation calls, authoritative/forced/cached checks, CPU, peak RSS and queue wait. Repeat across 10k/100k existing-photo libraries to separate wave-size from library-size scaling. Compare four and eight preparation workers only after establishing that page-coalesced reservations are active, and retain all durable completion/fencing checks.

This analysis generated no application changes and ran no synthetic production load. Conclusions are based on live operational telemetry and source inspection; a controlled current-image benchmark remains necessary to quantify the newest optimization's benefit.

## Follow-up implementation: incremental maintenance source reads

Following comparison with the wave-indexing improvements, strict incremental lexical and People Embedding maintenance now use bounded, ordered, context-preserving parallel source reads. Default concurrency is four; setting `INDEX_SOURCE_READ_CONCURRENCY=1` restores serial reads. SQLite and publication remain on the owning thread. The page-reservation and full-scan improvements discussed above already serve their respective shared paths and were not duplicated.

A local simulated-latency experiment measured 3.88× and 3.12× speedups in those source stages, with identical output hashes and unchanged source call counts. This is not a whole-job or Azure speedup. 278 focused tests passed. See `docs/benchmarks/maintenance-source-reads-20261106/README.md` and its raw results for the workload and limitations. These changes have not been deployed by this analysis.
