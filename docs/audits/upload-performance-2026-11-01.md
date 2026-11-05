# Upload performance — 2026-11-01

Read-only inspection of Azure Log Analytics workspace `forenklaprep1-dev-law`
(`6ca9803c-dd23-4393-a80a-c1fe035e7df9`) and Azure Monitor metrics for
`forenklaprep1-dev-upload`. No deployment or library changes.

## Batch outcomes

The latest two browser batches match the user's approximate 100/1,200 description;
the actual selections were 90 and 1,270 photos, with no videos.

| Batch | Stockholm time | Selected | Uploaded | Duplicates | Failed | Elapsed | New photos/min |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| `12bc711095f041c7a35fa1e082b8ee99` | 11:09:10–11:10:09 | 90 | 90 | 0 | 0 | 58.662 s | 92.1 |
| `27e4fb53d73945ec81cd95d6a356185a` | 11:10:58–11:18:08 | 1,270 | 1,261 | 9 | 0 | 430.248 s | 175.9 |

These are browser end counters, not a fresh unique storage inventory.
Finalization telemetry contains replays: the larger batch logged 1,271 successful
file finalization attempts (including five duplicate attempts), so this must not
be interpreted as unique uploaded files. Finalized source attempt volume was
44.52 MB and 1,131.82 MB respectively; direct network bandwidth is not measured.

## App performance

Outer `PERF event=request` logs include admission work after the inner upload
timer, and are the appropriate source for complete server request durations.
The 10:09–10:19 UTC interval contains the two batches.

| API | 90-photo average / p95 | 1,270-photo average / p95 |
| --- | --- | --- |
| init-batch | 440 / 677 ms | 235 / 403 ms |
| finalize-batch | 1.96 / 3.31 s | 1.65 / 3.62 s |

The larger batch made 1,034 init-batch and 710 finalize-batch requests. Finalize
batch size averaged only 1.79 files. The smaller batch made 72 init and 62 finalize
requests, averaging 1.45 files per finalize batch.

There were three finalize HTTP 503 responses in the larger batch, at 10:11:46,
10:13:18 and 10:13:19 UTC. The longest request took 7.44 s. The exception chain
shows Azure Table `UpdateConditionNotSatisfied` → `MutationContention` →
`Wave finalize admission must be retried`. The browser ultimately reported zero
failures. Two resolve-batch requests also returned 409; those are separate from
the three finalize retries.

Inner upload timing records report all requests successful and exclude these
outer admission failures; their error counts alone would be misleading.

Scaling rose to 2, 3, 4 and finally 5 replicas; the app was at its configured
five-replica maximum from approximately 10:13 UTC. Each replica has 0.5 CPU and
1 GiB memory. Azure metrics show no restarts, memory at most 36%, and CPU minute
averages around 31–38% during 10:13–10:18 UTC. CPU briefly peaked at 100% during
scale-up. This does not suggest sustained CPU or memory exhaustion.

Across both batches, finalize requests averaged 133 storage calls, 1.43 s
attributed storage I/O and 1.67 s outer request time. I/O attribution can include
overlapping work. Repeated policy, wave item and metadata reads are visible in
duplicate-I/O warnings. Storage coordination and small effective batch sizes are
the clearest optimization candidates; increasing replica count alone may add
contention. The measured larger-batch photo rate was 1.91 times the smaller
batch's, but file sizes and the warm/scaled app differ, so this is not a controlled
benchmark.

Throughput samples emitted during 10:09–10:19 UTC report 1,168.49 MB over
2,220.061 replica-seconds: 1,894.8 MB/hour/replica. Sample windows cross the
interval boundaries and count accepted attempts, so this is neither unique
library growth nor browser transfer bandwidth.

Browser preview and thumbnail receipts were accepted on all nonduplicate
finalization attempts. Two larger-batch metadata packets were not submitted
(`metadata_unsupported` / `unqualified_family`); these did not fail the originals.

Upload completion does not establish downstream completion. The smaller wave
`03497b116252481fa27cc95d48884b04` has 90 successful Vision image summaries and
a successful Vision attempt at approximately 10:11 UTC. Full clustering and
publication completion for both batches was not established by this inspection.
