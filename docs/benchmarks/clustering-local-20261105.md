# Local clustering benchmarks — 2026-11-05

Page coverage and concurrent frozen-person reads reduce counted calls by about 63%, but the zero-network local wave benchmark shows modest overall gains: 1.05× at 500 faces, 1.12× at 1,902, and 1.01× at 10,000. A separate fixed-latency model produces 2.21× at 500 faces. The latter is a simulation, not an Azure throughput prediction.

## Method

Compared the same workspace implementation with both optimization flags disabled versus enabled, in the same process. Real FAISS, 512-dimensional synthetic orthogonal vectors, 32 pre-existing identities, canonical assignment, local SQLite, mounted-journal code and real local fsync calls are used. The vectors repeat within each identity; these results do not characterize real face distributions, clustering quality or large-library candidate search. Each size has three before/after pairs, with mode order alternating and a small unmeasured warmup. Setup, data seeding, allocation cleanup and output comparisons are outside the runner timer.

The timer covers fresh frozen assignment through its completion receipt, including baseline preparation, matching, materialization and local journaling. Counted calls include source, admission, coverage and wave-control table fake operations; nested fake transaction internals count once. Upload, Vision, external immutable-receipt fetching, stage claim/release, downstream Index and final wave publication are excluded. The wave-ownership callback is constant, so production policy-read cost is excluded. Native runtime SQLite/fsync work is timed but is not counted as a table call.

All measured pairs produced identical ownership, person membership, representative and member snapshots. Current HEAD at report time is `c7d9d7f`; the benchmark compares flags in the local working tree rather than separate checked-out commits. No Azure requests, deployment, queue changes or production data writes were performed by these local benchmarks.

## Zero added storage latency

Medians of three runs; per-mode ranges and CPU times are preserved in the raw JSON.

| Faces | Before | Optimized | Speedup | Counted calls before → after |
|---:|---:|---:|---:|---:|
| 1 | 0.039 s | 0.039 s | 0.98× | 41 → 34 |
| 128 | 0.557 s | 0.538 s | 1.03× | 2,463 → 932 |
| 500 | 2.321 s | 2.214 s | 1.05× | 9,557 → 3,577 |
| 1,902 | 9.078 s | 8.080 s | 1.12× | 36,295 → 13,546 |
| 10,000 | 50.689 s | 50.027 s | 1.01× | 190,736 → 71,131 |

At 10,000 faces, optimized median throughput is approximately 200 faces/second on this machine and fixture. The per-write median is approximately 197 faces/second. These are local runner rates, not upload-to-complete rates or service capacity estimates.

## Storage-latency sensitivity model

This separate run adds a requested 1 ms sleep per outer fake table call, including reads, writes and transactions. Concurrent queries can overlap their sleeps. OS scheduling can overshoot the requested delay. This does not simulate bandwidth, retries, throttling, SDK serialization or Azure service behavior.

| Faces | Before | Optimized | Simulated speedup |
|---:|---:|---:|---:|
| 1 | 0.095 s | 0.084 s | 1.12× |
| 128 | 4.231 s | 1.824 s | 2.32× |
| 500 | 17.917 s | 8.092 s | 2.21× |

The result demonstrates sensitivity to per-call waiting; it does not establish a production speedup multiplier.

## Frozen-person query concurrency

A separate helper benchmark uses 56 keys, the real `read_entities` implementation, 512D payloads, five alternating trials and exact result parity. Both modes issue exactly four queries. At a requested 5 ms delay per query:

| Rows | Serial fourteen-key windows | Concurrent 56-key window | Speedup |
|---|---:|---:|---:|
| existing | 25.53 ms | 6.70 ms | 3.81× |
| missing | 25.55 ms | 6.86 ms | 3.73× |

This confirms bounded overlap without increasing query count. It is separate from whole-wave throughput.

## Remaining local costs

Median inclusive phase elapsed at 10,000 faces. Phases can overlap and their independent medians must not be summed into a wave total.

| Phase | Before | Optimized |
|---|---:|---:|
| snapshot_prepare | 1.473 s | 1.575 s |
| assignment_persistence | 3.715 s | 2.527 s |
| journal_write | 17.345 s | 18.034 s |
| matching | 15.206 s | 15.760 s |
| runtime_update | 8.416 s | 8.605 s |

Both 10,000-face modes perform **20,002 journal fsyncs** and **30,000 person/member/face source writes**. Optimized coverage uses 79 pages and 158 coverage transactions, versus 20,000 coverage transactions before. Matching, runtime updates and durable per-face journaling remain substantial local work. Their costs are not removed by coverage batching.

Near-zero total remote calls remain incompatible with this materialized storage path: live observations, admission and source writes are still proportional to face count. Further local speed work should profile journal serialization/durability and candidate search while preserving write-ahead recovery; the next deployed throughput decision still requires a controlled run with complete stage timings and storage attribution.

Full 100,000-face execution was **not measured**. The measured range is 1–10,000 faces with balanced identities. It does not prove capacity for concentrated memberships or different existing-library sizes, and no 100k timing is extrapolated.

## Harness correction and validation

The original coverage test fake deep-copied its full ETag history for every transaction, introducing artificial quadratic time as deleted reservation IDs accumulated. Those preliminary timings were discarded. The benchmark-only `BenchmarkTable` snapshots just transaction participants and restores their rows/versions on failure. Atomic commit and partial-failure rollback were compared with the original fake; results match. This correction changes no production storage code.

The final data contains **48 measured whole-wave executions**, all paired with exact assignment parity, plus the isolated read trials. The targeted journal, coverage and table-read suite passes **56 tests**. Python compilation, result completeness/query-count checks and `git diff --check` pass.

## Reproduce

```sh
.venv-photostore-tools/bin/python backend/scripts/benchmark_clustering_local.py --sizes 1 128 500 1902 10000 --repeats 3 --output docs/benchmarks/clustering-local-20261105.json
.venv-photostore-tools/bin/python backend/scripts/benchmark_clustering_local.py --sizes 1 128 500 --repeats 3 --latency-ms 1 --output docs/benchmarks/clustering-local-latency-20261105.json
.venv-photostore-tools/bin/python backend/scripts/benchmark_cluster_person_reads_local.py --output docs/benchmarks/clustering-local-person-reads-20261105.json
```

Raw results: [local wave runs](clustering-local-20261105.json), [latency sensitivity](clustering-local-latency-20261105.json), [isolated person reads](clustering-local-person-reads-20261105.json).
