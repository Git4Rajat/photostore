# Clustering coverage and baseline-read follow-up — 2026-11-05

Implemented locally on source commit `1f5e426`. No production source data, queue, infrastructure or deployment was changed. The Azure lookup experiment was read-only. This addresses the remaining costs identified in the [post-rollout audit](../audits/clustering-post-rollout-throughput-2026-11-05.md).

## Changes and compatibility

Assignment pages now share one lazy people-embedding coverage reservation across up to 128 canonical faces, with a hard bound of 256 face/person mutation attempts. The existing CoverageStore `uncertain=True` protocol sets `fullRequired` before any effect, conservatively covering every old/new owner. The existing wave-owned per-write branch already used this conservative coverage mode. This change does not introduce a storage model or change matching policy.

Only the exact face/person SourceTable proxies, library, originating thread, active wave-source context, person creates and genuine conditional updates can join. Unsupported operations use ordinary tracking. Live curation observations, original source ETags, library/execution ownership, per-effect operator admission, source-write order, per-face durable decisions and applied markers remain. Redundant coverage-layer source rereads are removed; the assignment adapter still observes live rows. Coverage closes before page progress, snapshot or yield publication. Empty/replayed pages create no reservation.

Unknown begin replies retain their possibly committed reservation without executing source effects. Finish failures remain visible, preserve the original source exception when present, and prevent progress publication. Paused/orphaned reservations never expire; existing operator quiescence recovery remains necessary after an ambiguous begin or unrecoverable interruption. Ordinary foreground writes and other threads retain separate reservations. Batching increases the amount of work protected by one reservation, without claiming cross-table atomic writes.

Frozen person preparation now uses bounded 56-key windows, allowing four concurrent fourteen-key queries through the existing globally bounded table-read pool. This applies only before the frozen manifest is published. Matching, journal format, recovery receipts and live write observations are unchanged. There is no cross-wave cache or reuse of mutable rows. The baseline candidate buffer increases from 14 to at most 56 records; whole-library state remains in SQLite. Small waves dispatch immediately without waiting to fill a window.

Independent rollback switches, both enabled by default:

- `WAVE_CLUSTER_COVERAGE_BATCH_ENABLED=false`: restore ordinary per-write coverage.
- `WAVE_CLUSTER_PERSON_READ_BATCH_ENABLED=false`: restore serial fourteen-key frozen-person windows.

New phase timings: `coverage_begin`, `coverage_finish`. New summary counters: `cluster_coverage_pages`, `cluster_coverage_writes`, `cluster_coverage_finish_errors`.

## Measured evidence

The reproducible offline benchmark runs the real FAISS assignment path and CoverageStore with ETag-bearing fake tables against identical 500-face inputs and the same baseline. Assignment outputs match exactly.

| Counted operation | Per-write | Page coverage |
|---|---:|---:|
| Coverage transactions | 2,000 | 8 |
| Coverage reads | 2,001 | 9 |
| Face/person live and tracking reads | 2,000 | 1,000 |
| Admission reads | 2,000 | 1,004 |
| Person/member/face writes | 1,500 | 1,500 |
| Total counted calls | **9,540** | **3,560** |

This is a **62.7% reduction within the measured scope**, not a production latency prediction. Counts exclude journal/control storage, immutable receipt reads, network bytes and the production wave-policy lookup (the fixture uses a constant callback). Nested fake transaction internals are counted once; counters are thread-safe. The person query count is unchanged in this fixture: concurrency reduces waiting rather than the number of fourteen-key queries. Source writes remain proportional to faces; near-zero total calls require a separate authority/storage migration.

A separate bounded Azure comparison sampled 56 existing people plus 56 synthetic missing keys, using three trials with rotating mode order and exact full-row result parity. Each query mode made four helper requests per trial; parallel point reads made 56. Median existing-person time was **1,111.9 ms serial queries**, **328.1 ms concurrent queries**, and **859.8 ms concurrent points**. Missing-key medians were 935.3, 64.3 and 517.3 ms respectively. Samples include substantial outliers; these are local-client lookup timings, not cluster-container timings or an end-to-end speedup forecast. The evidence favors bounded concurrent queries over switching to per-key point reads.

Reproduce from the repository root using the tools virtualenv:

```sh
.venv-photostore-tools/bin/python backend/scripts/benchmark_wave_coverage.py --faces 500
.venv-photostore-tools/bin/python backend/scripts/benchmark_cluster_person_reads.py --account ACCOUNT --library LIBRARY --keys 56 --trials 3
```

Raw non-payload results: [offline call comparison](clustering-page-coverage-20261105.json), [Azure lookup samples](clustering-person-reads-20261105.json). The Azure script requires authenticated read access and prints no person payloads or IDs.

## Validation and rollout acceptance

The final targeted suite passed **348 tests** in 5.64 seconds, including the progress-finalization failure case. Python compilation and `git diff --check` also pass. Coverage tests exercise old-capture invalidation, concurrent/paused writers, admission fences during begin and between writes, ambiguous begin/finish, failure chaining, scope isolation and hard bounds. Journal tests prove the serial and parallel paths publish byte-equivalent frozen records for 137 inputs and 113 baseline candidates, including a pre-existing deterministic person. Oracle, curation, recovery, continuation, lease and table-read failure-draining tests remain green.

After deployment, repeat a fixed-input/baseline run and compare whole-wave elapsed, newly resolved/replayed counts, total attributed storage calls and snapshot/persistence phase timings. Expect roughly one coverage page per 128 faces that actually write, not one reservation per mutation. Verify the deployed image and rollback configuration. Do not claim a throughput multiplier until that controlled run completes.

## Subsequent local timing benchmark

The [repeated local benchmarks](clustering-local-20261105.md) now cover 1–10,000 faces with real 512D FAISS, local journaling and instrumented wave-control calls. Zero-added-latency median speedups are 1.05× at 500 faces, 1.12× at 1,902, and 1.01× at 10,000. A separate requested-1-ms-per-call simulation produces 2.21× at 500 faces. All assignment outputs match. These results distinguish call reduction from local elapsed improvement and remain outside a production throughput claim.
