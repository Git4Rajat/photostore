# Maintenance source-read concurrency — November 6, 2026

Wave indexing already uses ordered parallel scans, context-preserving preparation workers, and page-level coverage reservations. Full-scan changes in shared `storage_utils` also benefit maintenance. The remaining strict incremental lexical and People Embedding implementations fetched affected rows serially.

These paths now share `index_source_reads.SourceReads`: four read workers by default, ordered results, bounded prefetch, and a separate context copy for every task. People and face lookups reuse one executor; SQLite writes, vector construction and manifest publication stay on the owner thread. Reads already in flight drain before the owning lease/publication context exits. Source admission checks, revision certification, ETag publication and dirty acknowledgements retain their existing behavior.

Set `INDEX_SOURCE_READ_CONCURRENCY` from 1 through 16. The default is 4; 1 restores serial reads. This is per maintenance merge, not a replica or publisher count. Prefetch is bounded to the worker count per active iterator. Nested person/face iterators can retain two windows, while active read threads remain capped at the configured worker count. This increases the constant source-row memory window but does not hydrate the base index.

## Local experiment

Run from the repository root:

```sh
.venv-photostore-tools/bin/python backend/scripts/benchmark_maintenance_source_reads.py \
  --output docs/benchmarks/maintenance-source-reads-20261106/results.json
```

Three samples per configuration, simulated 5 ms table-read delay, actual incremental source-processing functions:

| Workload | Serial median | Four-worker median | Source-stage speedup | Reads per run |
|---|---:|---:|---:|---:|
| Lexical: 256 changed photos | 1.855 s | 0.478 s | 3.88× | 256 |
| People Embedding: 32 people, 8 faces each | 2.101 s | 0.673 s | 3.12× | 288 |

Outputs have identical SHA-256 hashes across all serial/parallel runs, and source call counts are unchanged. The experiment isolates overlapping I/O. It does not simulate Azure throttling, full artifact download/rewrite/upload costs, or whole-job throughput. Person distributions with few faces may gain less. No production speedup is claimed and no deployment was performed.

## Correctness validation

278 tests passed across the new source-read tests and the existing lexical incremental, People Embedding incremental, automatic runtime, automatic coverage and maintenance worker suites. Coverage includes ordered overlapping reads, isolated context propagation, bounded lookahead, nested lookups, failures before publication, membership/read budgets, source revisions, lease loss and ETag publication failures. Added integration cases verify that a failure among concurrent source/face reads leaves existing blobs and manifests intact.

The next live comparison should hold library size and dirty-set distribution constant and report complete maintenance-job wall time, source calls, CPU, RSS and throttling. Do not generalize these source-stage ratios to the whole container.
