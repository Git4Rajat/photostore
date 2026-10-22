# 1,000 active users with 100,000 photos each

2026-10-21. Assumes uncapped app scaling and suitably large instances, as requested. Actual measurements below are local macOS database measurements, not maximum-size Azure instance or distributed HTTP results.

**Conclusion: a plausible target with deployment tuning, but concurrent-user capacity remains unproven.** The structural storage-capacity issue at 100 billion photos is much smaller here: there are 100 million photos, or 400 TB of originals assuming 4 MB each, before derivatives and backups. Storage sharding is not automatically required by that logical capacity alone; throughput and operational requirements still need validation.

At one API request per active user every ten seconds, demand is 100 requests/s. At one per second it is 1,000 requests/s. A simultaneous burst of 1,000 requests is a separate workload. With 90% gallery / 10% search and 24 metadata rows per response, the respective fresh-row demands are approximately 2,400 and 24,000 returned entities/s, before scans, auth, index operations, writes and retries. These are planning assumptions, not measured traffic.

## Actual 100,000-photo library results

Build: 34.311 s; database: 64.89 MiB; gzip: 17.82 MiB.

| Operation | Warm p50 ms | Sample p95 ms |
| --- | ---: | ---: |
| gallery_capture_first | 0.054 | 0.068 |
| gallery_capture_middle | 23.903 | 25.643 |
| gallery_capture_tail | 45.807 | 57.854 |
| gallery_name_first | 7.042 | 7.164 |
| gallery_rating_first | 0.032 | 0.057 |
| gallery_filtered_first | 0.731 | 0.801 |
| search_beach_candidates_score_count | 527.367 | 532.885 |
| search_common_candidates_count | 63.013 | 65.972 |

Each gallery call returns 100 filenames, not 24 full summaries. Search includes candidates, lexical scoring and count; it excludes the complete HTTP search route. Twelve serial samples are insufficient for production tail latency estimates. The fixture contains no real photo bytes or OCR/face embeddings. No network, auth or Azure metadata-read timing is included.

| Shared-library threads | Operations/s | p95 ms |
| ---: | ---: | ---: |
| 1 | 14.838 | 530.735 |
| 4 | 11.242 | 707.781 |
| 16 | 14.072 | 1824.211 |

The 80-operation trials use one process and one shared library: 90% capture-date pages at varying offsets, 10% search. They do not establish 1,000-user capacity or a replica count. Python process/thread behavior, shared caching and local machine conditions affect these measurements. Search still scores up to 4,000 candidates, so its cost does not fall tenfold simply because the library is ten times smaller.

One uncompressed base per library across 1,000 libraries would total 68.04 GB for this fixture, before duplicates, updates and scratch space. Tune local disk/cache capacity and routing against active working sets rather than relying only on more CPU/RAM.

At 100,000 photos, the current default 200,000-row threshold permits downloading the browser sort/gallery index. This can reduce repeated backend gallery operations but introduces browser download, memory, sorting and IndexedDB costs that this backend benchmark does not measure. Browser performance, especially mobile, must be tested separately.

Next validation: deploy the chosen instance sizes with tuned Gunicorn processes and caches; run 1,000 distinct libraries under warm and cold starts at the intended request rate; include thumbnail/preview delivery, index freshness, upload/AI queues and failure recovery. Measure achieved RPS, p95/p99, errors, source-store throttling and cache churn. The previously prepared k6 harness is hard-coded to million-photo identities; adapt its expected fixture count before using it for this scenario.

[Raw results](library-100k-20261021.json). Reproduce with `backend/scripts/benchmark_library_scale.py --photos 100000 --samples 12 --output docs/benchmarks/library-100k-20261021.json` using the backend Python environment.
