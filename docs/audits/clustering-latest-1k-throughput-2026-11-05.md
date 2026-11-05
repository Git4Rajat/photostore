# Latest 1,000-file upload — 2026-11-05

The latest run completed successfully at **16:40:46 Europe/Stockholm**. Its clustering throughput was **4.75 resolved face records/second**, versus **2.08** for the preceding near-1,000-file run in the same library: an observed **2.28× improvement**. Total time from the first Vision attempt to final Index completion fell from **25m04s to 20m53s**, about **16.7% shorter**. This interval excludes the upload itself and any waiting before Vision started.

## Run identification

- Latest wave: `98d5fb28feb442409e36ce63c16d5ef9`; 1,000 member records fetched, 1,000 Vision image summaries, 1,002 frozen/resolved face records.
- Previous comparison: `00e6cb68cb904e7a900a48264944f14b`; 997 member records fetched, 992 frozen/resolved face records. Both waves have the same hashed library identity.
- Live cluster revision: `forenklaprep1-dev-cluster--0000005`, matching the latest run's telemetry. App health is `Succeeded` / `Running`; image is `ghcr.io/git4rajat/photostore-backend:8f1ade3fbd5524dd1565946fbc6c16c1cb6cfb24`, with 2 CPUs and 4 GiB memory. That image includes clustering commit `4f73975` and subsequent indexing changes.
- Previous clustering ran on revision `--0000004`; latest Vision ran on `--0000005`, Index on `--0000009`. The comparison therefore includes other deployment changes as well as clustering optimization.

## Measured comparison

Stage durations sum recorded attempt elapsed, including normal continuation attempts. The final row measures wall time across the processing interval using application telemetry timestamps, including stage handoffs.

| Metric | Previous near-1k | Latest 1k |
|---|---:|---:|
| Member/file records | 997 | 1,000 |
| Resolved face records | 992 | 1,002 |
| Vision attempt elapsed | 10m35s | 11m00s |
| Clustering attempt elapsed | **7m57s** | **3m31s** |
| Clustering throughput | 2.08 records/s | **4.75 records/s** |
| Cluster attributed storage calls | 22,319 | **11,029** |
| Cluster calls per resolved face | 22.50 | **11.01** |
| Clustering attempts | 3 | 2 |
| Index attempt elapsed | 5m31s | 4m59s |
| Vision start → Index complete wall time | **25m04s** | **20m53s** |

Clustering elapsed is 55.7% lower despite resolving slightly more face records. Storage calls per resolved record fell **51.1%**. The earlier 500-file run remains another useful reference, but the immediately preceding near-1k run is closer in size.

`cluster_faces_newly_resolved` is a durable journal-progress measure and includes valid preserved, excluded and curated outcomes. It is not a count of newly assigned faces or newly created people. These throughput figures use resolved records consistently.

## Where clustering improved

| Inclusive cluster phase | Previous | Latest |
|---|---:|---:|
| Frozen snapshot preparation | 203.73 s | **74.44 s** |
| Assignment persistence | 194.83 s | **72.60 s** |
| Journal writes | 43.65 s | 40.83 s |
| Matching | 3.99 s | 4.34 s |

Phase measurements overlap and must not be summed into another elapsed total. Persistence attributed calls fell from 17,236 to 6,566. Snapshot phase attributed calls changed only from 3,079 to 2,965 while elapsed fell substantially; bounded concurrency and different service/workload conditions affect that result, so call reduction alone does not explain it. The initial snapshot summary records four peak in-flight batch reads.

The page-coverage counters demonstrate the optimization was active: **8 coverage pages protected 1,756 face/person writes**, with **zero coverage-finish errors**. The continuation restored its saved runtime once, and **zero faces were replayed**. Frozen membership needed two fetches for 1,000 records. Matching was about 2% of cluster execution and is not the remaining dominant live cost.

All latest attempt summaries report success; source-error counters, storage throttles, server errors and coverage-finish errors are zero. There are no `wave_stage_failed` events in the wave's event inventory. Recorded retry scheduling corresponds to bounded continuations (six Vision, one Cluster, two Index), not failed attempts. Cluster storage-error counters consist of expected not-found responses and two create conflicts, with no precondition failures; they should not be interpreted as failed processing.

## Remaining bottleneck and limits

Vision is now the largest stage at approximately **11 minutes**, followed by Index at **5 minutes**, then Cluster at **3.5 minutes**. Within clustering, baseline preparation and persistence remain the largest measured phases. The optimization materially improved live clustering, while the total processing gain is smaller because Vision did not improve in this comparison.

These are observed production runs, not identical-input/frozen-baseline A/B tests. Input counts, face outcomes, existing-library contents, deployment components and service timings can differ. The data supports an observed 2.28× clustering-throughput gain and a 16.7% shorter processing interval; it does not establish those as guaranteed future multipliers or forecast 100k-face performance.

Evidence was read from the linked Log Analytics workspace and the live Container App. Raw query responses, phase breakdowns and normalized analysis are retained privately under `.azure/clustering-latest-1k-20261105/`. No production data, configuration, queue or deployment was changed during this check.
