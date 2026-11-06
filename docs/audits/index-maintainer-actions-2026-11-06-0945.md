# Index maintenance after the morning UI actions — November 6, 2026

The actions around 09:45 Stockholm time triggered successful small incremental builds. The new parallel-read implementation was deployed, but these jobs did not exercise its lexical-artifact or People Embedding paths. These logs validate normal maintenance behavior; they cannot establish a production speedup for the new source reader.

## Image and time verification

All sampled indexer logs belong to revision `forenklaprep1-dev-indexer--0000019`. System logs show both containers pulled backend image `f565350c2abf6036701018f09ff922dc8abb1cdd` before the actions. Git ancestry confirms that image commit includes `aaf27cc`, the maintenance concurrency change. Current configuration confirms that image for both the API and maintenance container. There is no explicit `INDEX_SOURCE_READ_CONCURRENCY` override, so the code default is four.

Times below are Stockholm local time (CET, UTC+1). The initial indexer query covered 09:30–10:30; a follow-up checked builds, outcomes and errors through 11:00. The user's mutation requests appear mainly at 09:40–09:44: creating an album, adding/removing photos, deleting/restoring photos and an album, merging people, auto-creating albums and invoking Tools actions. Those mutation requests returned successful responses. Read-only logs/configuration were inspected; no actions or builds were triggered by this investigation.

## Completed maintenance work

Times are build scope-summary timestamps, near the end of each build. Durations exclude waiting for queue delivery and startup.

| Local time | Scope | Build duration | Storage calls in build scope | Outcome |
|---|---|---:|---:|---|
| 09:41:38 | Albums | 423 ms | 24 | intent_completed |
| 09:41:39 | People | 1,283 ms | 83 | intent_completed |
| 09:42:06 | Albums | 162 ms | 14 | intent_completed |
| 09:42:07 | People | 1,148 ms | 77 | intent_completed |
| 09:43:21 | Light: Sort/Access | 2,447 ms | 87 | intent_completed |
| 09:43:59 | Albums | 207 ms | 14 | intent_completed |

Total recorded build-scope time: 5.67 seconds. Each of the six main-queue handlers completed on dequeue count one. The corresponding main-queue handler durations, including handler overhead, were 556, 1,411, 342, 1,290, 2,620 and 354 ms. Deferred-queue forwarding messages are separate and must not be counted as additional builds.

The first Albums build targeted three albums; later Albums builds targeted one each. The first People build explicitly reports `people_index_incremental_by_ids`, affecting two people. People builds published one Search deletion and one Search upsert respectively, each with no remaining dirty rows. The light build reported Search `noop`. These are catalog People and Search-database updates, distinct from strict People Embedding and lexical JSON artifact merges.

No index build errors, tracebacks or retry outcomes appeared in the extended indexer query. No `lexical_artifact`, `people_embedding` or full-wave build appeared through 11:00. Accordingly there is no live before/after comparison or exercised source-read concurrency to report from this session.

## What cost time

For the two People builds, metadata reads remain the largest reported I/O category: 42 reads / 721 ms and 41 reads / 682 ms. Their total build I/O times were 1,102 and 1,086 ms, close to their 1,283 and 1,148 ms elapsed durations. These calls belong to existing People/Search maintenance, rather than the newly changed strict artifact reader.

The separate person-merge HTTP request at 09:42:11 took 2,706 ms server-side, with 128 storage calls and 2,019 ms reported storage time. It performed source updates and invalidation; it is not a measured People Embedding maintenance build.

Waiting/startup was more noticeable than the small builds. System logs record queue activation at 09:41:11; first main-queue processing began at 09:41:37.868, about 27 seconds later. The initial album creation/addition requests finished at 09:40:32, roughly 66 seconds before the Albums completion. The exact per-edit enqueue timestamp is not present, and coalescing combines multiple changes, so this is an observed action-to-completion interval rather than a precise queue-latency measurement. A second activation at 09:42:22 was triggered by HTTP demand after scale-down.

Worker recovery passes took 1.29–1.64 seconds. Maintenance RSS samples/scope peaks reached about 212 MiB. These small jobs showed no resource saturation. Startup emitted localhost connection-pool warnings, but the sampled builds still succeeded; the warnings alone do not establish a storage bottleneck or failure.

Core logs explain three inline Albums deferrals with `reason=search_source_dirty`; queued Albums work subsequently completed. Deferral here was expected handoff behavior, not a lost edit or failed build.

## Conclusion

The new image handled these actions successfully and quickly once jobs began. The sample did not invoke the two merge implementations changed by `aaf27cc`, so the earlier local source-stage speedups remain unverified in Azure. The next relevant observation is an actual incremental `lexical_artifact` or `people_embedding` maintenance job with its affected-ID count and full build-scope timing. For these UI actions specifically, queue/startup latency and the existing People/Search metadata reads are more material than strict artifact source-read concurrency.

Raw evidence and KQL: ignored local directory `.azure/maintenance-actions-0945-20261106/` (`indexer.json`, `followup.json`, `core.json`, `actions.json`, `system.json`, `image-settings.json`, `queries.json`).
