# Maintenance CPU and memory reduction bundle — November 6, 2026

This bundle targets allocated compute during unnecessary co-wakes, native SQLite memory, redundant source work, and recurring background I/O. It does not lower the maintenance worker's own CPU/memory limits before representative large-library measurements.

## Changes to test together

| Change | Previous behavior | New behavior |
|---|---|---|
| Independent API/worker scaling | Either HTTP or queue demand starts 2 CPU / 4 GiB | API-only: 0.5 CPU / 1 GiB. Queue-only: 1.5 CPU / 3 GiB. Both active: unchanged 2 CPU / 4 GiB |
| SQLite reader cache | 16,000 KiB per connection | Worker default 4,096 KiB; API default unchanged |
| Photo membership projection | Read metadata, then read it again before updating | Reuse the captured row only with a matching identity and ETag; CAS conflict re-reads fresh source |
| Pending person labels | Repeat membership repairs for each person's photos | Deduplicate photo names in temporary SQLite, then repair at most 128 photos per page |
| Derived Explore/Timeline summaries | Upload and verify both outputs after every successful Search drain | Read existing outputs; skip writes only when snapshot identity and content match, ignoring timestamp-only churn |
| Artifact GC | Survey after each successful intent and every recovery sweep | Startup survey, then every 900 seconds while alive; no per-edit survey |
| Durable work recovery | Every 60 seconds while alive | Preserved; independently tested when GC is not due |
| Empty queue polling | Poll main and deferred queues every 2 seconds | Back off 2 → 4 → 8 seconds; reset to 2 after processing work |
| Queue-age logging | Used the absent `insertion_time` field on native SDK messages | Reads native `inserted_on`, retaining compatibility with old adapters |

Independent scaling saves 75% of the former paired reservation during API-only activity and 25% during worker-only activity. These are allocation comparisons, not whole-day cost or CPU utilization forecasts. Overlapping API/worker demand has the same combined allocation as before.

The membership path retains source reservations, source outboxes, conditional updates and dirty acknowledgements. SQLite writes and publication ownership are preserved. A newer photo edit or deletion defeats a captured ETag; the existing retry path re-reads before proceeding. Summary comparison is made against a real blob load under the existing catalog serializer; missing or different outputs are still published and verified.

GC remains best effort and runs only while a real worker is alive. A short-lived worker may stop before the next GC interval; its next activation performs another startup survey. No timer is added to wake compute for housekeeping. Reader grace, reference validation and the real catalog lock remain mandatory. Empty-poll backoff can add up to eight seconds of in-process queue detection delay; this trades some responsiveness for less idle polling.

## Local memory experiment

`benchmark_maintenance_efficiency.py` uses the actual `SearchDatabase` connection path against a synthetic local SQLite table with 24,000 2-KiB payloads. Each cache setting runs in three fresh processes and scans the table twice. Median extra RSS above the pre-connection process baseline:

| Cache setting | Median additional RSS |
|---|---:|
| 16,000 KiB | 20.17 MiB |
| 4,096 KiB | 5.66 MiB |

That is about 14.5 MiB less resident memory per warmed connection in this workload. Median measured CPU time was 83.75 versus 84.16 ms; the small sample does not establish a CPU difference. A smaller cache can increase reads for other access patterns. This is not a whole-container or Azure benchmark. Raw runs are in `sqlite-cache.json`.

Reproduce from the repository root:

```sh
.venv-photostore-tools/bin/python backend/scripts/benchmark_maintenance_efficiency.py \
  --output /private/tmp/maintenance-sqlite-cache.json
```

The tests also demonstrate one fewer metadata source read for a valid captured row, one repair per unique photo across labels, bounded repair pages, no uploads for unchanged summaries, and continued intent delivery without a GC survey.

## Deployment and rollback

`separateIndexWorker=true` is the Bicep default. The API remains at `-indexer`; the queue-only app is `-index-worker`, with container name `index-maintenance` and `APP_ROLE=index-worker`. It has no ingress/HTTP scaler, keeps one publisher replica maximum, shares the existing cache mount and storage account, and receives its own storage and optional Web PubSub grants. Both queue scalers transfer to that app. The main queue counts all messages to protect claimed work; deferred scaling counts only visible messages.

A normal image-only upgrade does **not** create this resource or transfer scalers. For an existing deployment, plan an infrastructure rollout using the existing deployment parameters and pinned release images. Drain the old consumer and remove its sidecar/scalers before enabling the separate consumer. Preserve all queues, ledgers, receipts, secrets, cache mounts and release bindings; verify the new managed identity's grants before relying on queue activation. Do not treat an unmodified eight-app ARM snapshot as a split deployment.

The image-upgrade helper now includes the optional ninth app in both pause phases and activation. Fresh inventory verification rejects duplicate `index-worker` containers. Configuration and residency checks recognize the separate worker as the index-stage owner and reject a co-waking API or HTTP-scaled worker. The release capability still binds the same runtime role images; adding an app does not add a protocol role.

For rollback, restore `separateIndexWorker=false` **and stop the separate consumer**; incremental ARM deployments do not automatically delete an omitted conditional resource. Never leave both publisher topologies running. Remove the drained, obsolete worker app before qualifying the restored sidecar topology; inventory checks deliberately reject duplicate publisher containers even if one is currently stopped. Retain queues and durable intents throughout.

Tuning overrides:

- `SEARCH_DB_SQLITE_CACHE_KB=16000` restores the old reader cache.
- `INDEX_ARTIFACT_CLEANUP_INTERVAL_SECONDS=60` restores the old periodic GC frequency, without reintroducing per-edit GC.
- `INDEX_WORKER_IDLE_POLL_MAX_SECONDS=2` restores fixed two-second empty polling.
- The earlier `INDEX_SOURCE_READ_CONCURRENCY=1` override restores serial strict lexical/People Embedding source reads.

No cloud resource changes or deployment were performed as part of this bundle. Local Bicep compilation is not live policy/RBAC validation. Test a name change affecting overlapping photos, deletion/restoration, ordinary album edits and an actual strict artifact merge; collect metrics separately for `-indexer` and `-index-worker`. Compare full job CPU, RSS, source calls, queue age and aggregate replica time, including cold starts and cooldowns.

## Validation

768 focused tests passed across maintenance, source/lease/publication fencing,
People metadata, summaries, SQLite synchronization, and deployment/qualification
helpers. The additional paging test verifies 1,000 shared photos are repaired
once in pages of no more than 128. Main and sandbox Bicep compilation passed,
and the generated `deploy/azuredeploy.json` was refreshed. The compiler reports
storage API type-availability and existing optional-workspace/nullability warnings;
these compilations do not prove live Azure policy or permission readiness.
