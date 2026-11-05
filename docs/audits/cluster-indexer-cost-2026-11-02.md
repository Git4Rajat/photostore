# Cluster and indexer cost investigation — 2026-11-02

Read-only live investigation of subscription `dxi-idp-sandbox`, resource group
`forenklaprep1-dev-rg`, West Europe. No deployment, scale changes, queue changes,
or data repair performed. Existing uncommitted application changes were not edited.

## Posted cost

Azure Cost Management ActualCost / PreTaxCost, EUR, queried October 24–November 2.
November 1–2 are incomplete: November 1 has almost no posted app compute despite
runtime evidence, and November 2 is still in progress. Do not interpret this as
an actual November 1 cost reduction.

| Resource | October 24–31 posted cost | October 24–November 2 posted so far |
|---|---:|---:|
| Cluster | €11.21 | €11.21 |
| Indexer | €13.69 | €15.63 |
| Log Analytics workspace, all apps | €8.60 | €12.14 |

The cluster/indexer app charges are overwhelmingly active CPU and memory;
request charges are not the driver. Workspace cost is a separate shared line,
not a charge that can all be attributed to these two apps.

## Live configuration

Both apps have minimum zero, maximum one, cooldown 60 seconds, polling 30 seconds.
Cluster reserves 2 vCPU / 4 GiB. Indexer also reserves 2 vCPU / 4 GiB in total:
API 0.5 / 1 GiB plus maintenance sidecar 1.5 / 3 GiB. They scale as one replica,
so either HTTP traffic or maintenance work starts both containers.

Both deferred queues are deployed with visible-only scaling. The original
claimed-work queues still count all messages, to protect in-flight work.
The old delayed-message false-start fix and 300→60-second cooldown change
are already live; recommending those again would not address the current problem.

Azure bills allocated resources while replicas run, including startup and
cooldown. At minimum zero, a running replica is above minimum and eligible
for active-rate billing even when application code is idle.
Reference: https://learn.microsoft.com/en-us/azure/container-apps/billing

## Cluster: expiry and housekeeping wake the expensive worker

The worker is responsible for more than face clustering: upload-session expiry,
artifact cleanup and library operations also run here.

October 31 system logs show 56 replica assignments, 47 KEDA activations triggered
by `photostore-upload-expiry`, and only 2 triggered by `photostore-clustering`.
Console logs show 633 `upload_session_expiry` messages versus 2 upload-wave stage
messages. November 1 has 46 expiry-triggered activations and 265 expiry messages,
versus 4 clustering-triggered activations. These are counts of deliveries and
activations, not counts of unique user uploads.

`backend/upload_session_lease.py` implements a renewable 300-second lease and
reschedules checks while the session is still live. Every due expiry check can
start the same 2-vCPU/4-GiB cluster app. Many cheap checks therefore buy a full
worker startup and scale-down tail. Recent hourly replica metrics indicate
roughly 1–4 aggregate replica-hours/day rather than a few minutes/day.
These integrated metrics are estimates, not invoice quantities, and do not
provide an exact per-job cost allocation.

Repeated deployments also contribute: October 24–31 system logs record 69
cluster revision-creation events and 89 indexer revision-creation events.
New revisions incur startup work; counts alone cannot assign their euro share.

## Indexer: current blocked-wave retry loop

November 2 through approximately 14:42 UTC:

- 28,573 index-build job-start log records and 28,478 build-failure records.
- The sampled failures are `automatic_index_updates.SourceBusy: Guarded wave
  must finish before automatic artifact seed`, including people embedding and
  lexical artifact scopes.
- 306 scaler activations triggered by the deferred index-maintenance queue;
  317 replica assignments in the earlier system-log snapshot.
- Approximately 11.47 aggregate replica-hours by 14:00 UTC from hourly metrics.
- Earlier log-ingestion snapshot: 700,123 console lines / 0.443 GB today,
  compared with 21,784 lines / 0.0134 GB on November 1.

Counts are log records, not deduplicated transactions. The exception appears
in both source and final exception lines, so counting its text twice would
exaggerate the number of attempts; failure records are the better estimate.

Code path:

1. `automatic_index_updates.run()` rejects automatic artifact work while the
   guarded wave tail is neither complete nor cancelled.
2. `_run_index_build_job()` logs this expected barrier as a failed build and
   emits a traceback.
3. `_run_index_maintenance_payload()` catches `SourceBusy`, releases with
   `deferred=True, retry_delay=10, retry_reason='barrier_wait'`, and sends another
   wakeup.
4. `WorkStore.release()` refunds the attempt for a deferred barrier wait.
   Consequently the poison-message attempt limit does not stop this loop.

A live or stranded wave can therefore sustain retries indefinitely. The
underlying reason that the particular wave remains pending was not established
by this cost investigation; do not clear its state or ACK/drop work based on
this report. Deferred queue isolation prevents invisible messages from holding
a replica up, but does not eliminate repeated due deliveries.

The API and maintenance worker also start together. Historical indexer HTTP
scale activations show browsing/API calls can incur the full allocation.
API telemetry reporting idle does not establish that the maintenance sidecar
was idle; its telemetry must be considered separately.

## Recommended order of remediation

1. Inspect the blocking wave and ensure it can finish or recover safely.
2. Defer automatic artifact work until a wave-completion signal, with a bounded,
   backoff-based recovery check; treat SourceBusy as waiting, without per-attempt
   failure tracebacks. Preserve durable maintenance intent and recovery.
3. Move upload expiry/housekeeping to smaller compute or a shared lightweight
   scheduler, retaining durable expiry, cancellation and abandonment semantics.
4. Consider independently scaling indexer API and maintenance worker, so an API
   request does not start the maintenance allocation.
5. Measure CPU/memory peaks before changing worker size. Keep minimum replicas zero.

No savings percentage is asserted: overlapping triggers, historical revisions,
invoice delays and startup accounting prevent a reliable exact attribution.

Raw cost, metric and aggregate-log evidence is in ignored local directory
`.azure/cost-audit-20261102/`. Queries were read-only. Temporary Azure credential
copies used to accommodate the local CLI filesystem sandbox were removed.

## Deeper investigation — November 2, approximately 15:10–15:15 UTC

This follow-up establishes the pending wave and separates telemetry defects,
nonproductive retries, HTTP demand, recovery work, and productive wave work.
It supersedes the earlier statement that the blocking wave was not inspected.

### Confirmed deployed versions and replica state

Indexer API and maintenance sidecar both run `e287fc3cb5b1075c4ccb9c0accef1a135fd7e46d`.
The frontend also runs that image revision. Indexer revision `0000079` is the only
active revision, healthy, with one replica. No evidence of leftover active
revisions or runaway horizontal scaling was found.

### The retry delay is recorded but not enforced

In the sampled hour, the maintenance container logged 6,382 index-build starts,
6,382 build failures, and 6,382 final `SourceBusy` exception lines. All sampled
scope summaries belong to one library and three scopes: vector, people_embedding,
and lexical_artifact. A separately sampled one-hour gap analysis found:

| Scope | Attempts | Median gap | Gaps below 10 seconds |
|---|---:|---:|---:|
| lexical_artifact | 1,987 | 0.674 s | 1,979 |
| people_embedding | 2,032 | 0.418 s | 2,021 |
| vector | 2,266 | 0.435 s | 2,258 |

Queries have slightly different rolling windows, so totals are not identical.
Gaps use timestamps inside log messages, not ingestion timestamps. Each scope
attempt spent about 30 ms inside the rejected build, plus queue, lease, status,
serialization and logging overhead outside that scope.

The deployed `WorkStore.claim()` checks the ownership lease but ignores
`notBefore`. An early/duplicate queue wake can claim the same intent, clear its
wake reservation, fail the source guard, and publish another delayed wake.
`request()` can also shorten a waiting intent's deadline when new source
revisions arrive. Deferred barrier attempts are refunded, keeping attempts at
zero and preventing the poison budget from breaking this pattern.

A deterministic, network-free replay used the exact deployed module from
`git show e287fc3:backend/index_work.py`, the existing ETag-aware test table,
and a frozen clock of 1000 with a retry deadline of 1010:

| Version | Stale deliveries | Early claims | Total replacement wakes including initial |
|---|---:|---:|---:|
| Deployed e287fc3 | 100 | 100 | 101 |
| Current local changes | 100 | 0 | 1 |

This confirms the existing local changes address early-claim churn. It does
not prove all future idle cost is eliminated: even correctly spaced ten-second
barrier retries can recur more frequently than the 60-second cooldown. Completion
signals plus slower bounded recovery are the remaining design lever.

### The wave is actively pending, not merely a stale job-status badge

The durable maintenance ledger has three nonfailed intents for the real library,
all with `retryReason=barrier_wait`, `retryDelaySeconds=10`, no durable owner in
the snapshot, and attempts zero/one. Their RowKeys and scopes were inspected.
The main queue reported approximate count zero, the deferred queue 22, and the
dead-letter queue 10. These counts include invisible messages and are not proof
of 22 duplicate maintenance intents; wave-stage receipts can also share a queue.
Message peeking was denied by Azure data-plane permissions. Table reads and queue
metadata reads succeeded; no messages were consumed, ACKed, or modified.

The library control points at tail wave `6d61cb0ac5214365995d4288b519ea16`:
state `processing_pending`, 10,207 members, three sessions, zero open sessions,
with progress updated at the snapshot. Its predecessor,
`852ae63d0d12447cbe79c618e94798f8`, has 1,898 members and had entered `indexing`.
`automatic.wave_pending()` checks the tail's terminal state, so automatic
maintenance remains blocked until the current wave sequence settles. This is
an actual source barrier; changing a job badge or dropping retry messages is
not a valid repair.

The ledger also contains failed intents with pseudo-library names `__system__`
and `clustering_maintenance`. They are already failed with zero wake reservations;
they are not the three active retrying intents. Their provenance is separate
cleanup work, not evidence that they currently sustain the replica.

### HTTP polling is a second independent source of demand

The four-hour query found these actual PERF request records (one per request):
274 successful `/api/jobs/status` requests from Gallery, eight from Albums,
two from Sharing and one from Person, plus boot/other requests. There were also
13 older 500 responses caused by `KeyError: lexical_artifact`.

A recent 15-minute sample contains 46 API requests alongside 1,650 failed index
attempts and one new upload-wave stage. The API is mostly between requests:
215 memory samples in a one-hour window all had `inflight=0`, with peak one.
A low instantaneous inflight count does not establish absence of HTTP demand.

Commit `42d7c6e` gates global job polling to mounted Tools. It is present locally,
but not in the live frontend image `e287fc3`. Because API and worker are sidecars
in one replica, these short browsing requests bring up the full 2-vCPU/4-GiB
allocation. HTTP-only running time cannot be assigned an exact euro share while
queue demand overlaps it.

### Telemetry reports false idle, and some real work does occur

`run_index_worker` registers `processing_diagnostics.worker('indexer')`.
The reused queue handler is decorated with
`processing_diagnostics.measured('cluster', 'message', state='processing')`.
Consequently worker-state telemetry under indexer stays idle/processing_seconds=0
while the per-message instrumentation reports work under cluster. This is why
the earlier worker-state summary alone looked like zero work for every replica.
Furthermore a handled/deferred message can report worker_phase outcome=success
because dispatch returns normally; that means a message was handled, not that
an index was published.

Two genuine index-wave completions were observed today at 11:32 and 12:20 UTC,
lasting about 25.9 and 16.8 minutes. They recorded 145,171 and 96,475 storage
calls respectively, with 87,343 and 58,553 job reads. These runs are productive
wave activity and cannot be categorized as empty wakeups. Subsequent wave work
also began. The core finding is heavy nonproductive maintenance churn alongside
real wave work, not that the indexer does nothing all day.

The worker's once-per-minute recovery sweep also tries artifact cleanup and
hits the active-wave guard: 58 such final exception lines in the sampled hour.
These add storage checks and tracebacks but are in-process background activity,
not an independent KEDA wake trigger. Empty two-second queue polls likewise do
not themselves generate HTTP demand or set a desired replica count.

### Remaining actions, without duplicating existing fixes

- Deploy/review the existing local early-claim, deadline-preservation and
  expected-SourceBusy status changes; no replacement implementation was made.
- Deploy the existing Tools-only frontend job-polling commit.
- Ensure guarded-wave completion wakes deferred maintenance; use slower bounded
  recovery rather than indefinitely polling the same barrier every ten seconds.
- Correct telemetry role attribution and distinguish handled, deferred and
  published outcomes. Keep productive wave-stage durations visible separately.
- Consider independent API/worker scaling after removing the accidental demand.

No application code, cloud settings, queue contents, or source data were changed in this
follow-up. New evidence and query bodies are in `.azure/cost-audit-20261102/deep/`;
the deterministic replay script is in `/private/tmp/photostore-cost-evidence/replay.py`.
