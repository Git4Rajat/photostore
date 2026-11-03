# Vision failures: investigation and fix — November 3, 2026

Live read-only investigation of Azure logs, the stopped vision app's configuration, and paginated durable job ledgers. Local code changes and tests only. The deployed vision app remains stopped on image `d756032f2d5351b3f174dd015fc8541e2910db4a`, revision `0000062`. No deployment, app start, queue consumption/deletion, lease manipulation, or cloud data repair was performed.

## Findings

The observed wave failures are coordination failures after successful source processing. Repeated ETag conflicts at page-evidence commits exhaust the dispatcher budget, after which recovery excludes the failed intent. A separate duplicate tag-maintenance retry storm produces most of the error log volume and unnecessary worker demand.

This corrects the earlier association of nearby `Library assignment lease is busy` errors with the 1,898-item wave. Its own telemetry identifies page-commit conflicts, not a model or library-assignment failure.

### 1. Page commits depend unnecessarily on a shared control row

Production `wave_mutation_contention` records name `commit_observations.commit`, HTTP status 412, transaction index 0, after 16 internal CAS retries. In the deployed transaction, operation 0 is the library `control` row. The method captures control before reading member state and guards, then rewrites it even though it only needs to change the current wave's evidence.

Intake for subsequent waves and other library coordination can change that row during the read-to-commit window. These unrelated changes reject an otherwise valid page of already-qualified source results. A retry rereads the page, but also captures control early again, allowing sustained writers to starve it repeatedly.

Wave-specific evidence:

- Wave `852ae63d` (1,898 members): five failed vision attempts around 12:25–12:26 Stockholm all report page-commit errors and zero source errors. Corresponding conflict events identify the shared-control CAS. This precedes the long interruption.
- Wave `6d61cb0` (10,207 members): 12 measured failed attempts after 15:00 UTC; 11 report page-commit errors and one reports source-attempt reservation contention. All report zero source errors. Page failures recur across revisions `0000055`, `0000057` and `0000059`.
- The reservation conflict at 22:06 Stockholm is HTTP 412 at operation 1 of `reserve_attempts.commit`. Because the number of attempt-row writes varies, that index alone does not identify the conflicting row. Independently, inspection shows its policy guard was captured before several remote ownership/eligibility reads; the patch shortens that exposure.

The specific competing request that won each production control-row CAS was not logged. The telemetry identifies the rejected operation; code inspection and deterministic interleaving reproduce how successor intake causes it. This is stronger evidence than timestamp proximity to deployments, but not a per-request reconstruction of every writer.

### 2. Retry accounting converts intermittent contention into a stranded wave

`wave_runtime.consume()` charges coordination conflicts to the same five-attempt dispatch budget. Successful continuation refunds only the current claim, retaining failures from earlier pages. Therefore intermittent conflicts accumulate over the whole large wave, even between periods of durable progress.

The live `upload-wave-stage-pending` record for `6d61cb0` has:

- `failed=true`, `attempts=6`;
- no owner and `leaseUntil=0`;
- `failedAt` corresponding to November 2 at 22:38:20 Stockholm;
- `retryReason=progress` retained from the preceding continuation.

The next claim exceeds the ceiling before entering measured vision work and logs `Wave dispatch attempts exhausted; operator retry required`. Normal recovery skips failed intents. The one-hour stale-dispatch revival is opportunistic, so it can resume only when a later recovery event actually runs. This accounts for the abrupt stops and multi-hour gaps; running the app does not imply the failed wave is executing.

### 3. Duplicate tag-maintenance messages are treated as failures

For November 2 16:00 through November 3 02:00 Stockholm, the vision app emitted:

| Evidence | Count |
|---|---:|
| Console log lines | 3,168,012 |
| Final tag-maintenance failure log records | 58,049 |
| Final duplicate-claim exceptions (`WorkLeased` or its older RuntimeError form) | 58,040 |
| Final library-lease exceptions | 9 |
| Final exhausted-wave-dispatch exceptions | 3 |

These are log-record counts, not unique failed jobs. The duplicate exceptions account for all but nine tag-failure records in this window.

The tag handler catches a duplicate live claim in its generic exception block, emits a traceback, and returns `tag_index_retry`. The queue loop retains and re-delays every receipt, so a duplicate population persists rather than being acknowledged as obsolete. Early `notBefore` deliveries were also retained independently.

Additionally, the handler acquired/waited for the shared catalog lock before discovering that an upload wave already prohibited tag rebuilding. This wastes a worker slot and can emit lease errors for work that should have been deferred before lock acquisition.

This explains the maintenance error storm. The evidence does not prove those duplicates directly caused every control-row conflict; they are a separate confirmed defect and competing worker/storage demand.

## Deployment and infrastructure checks

The initial 1,898-item failure burst ended at 12:26 Stockholm. An existing-revision replica stop/restart followed at 12:31. New revision `0000055` was created at 14:44 and its main container started at 14:46; the wave resumed at 15:39:52. Deployment startup therefore does not explain the beginning or full duration of that interruption.

Across later sampled shutdowns, all but one reported zero in-flight messages; one reported one draining message. The shutdown-log query found no watchdog-timeout/OOM marker. The failed-attempt summaries recorded zero source errors and no storage throttling/server errors. These observations support the coordination/retry diagnosis; they are not a claim that deployments never interrupt work or that every possible infrastructure failure was ruled out.

## Implemented fix

1. `wave_source_batch.commit_observations()` atomically commits the member changes, current wave, execution guard and cleanup-policy guard without rewriting library control. Member/wave ETags still reject concurrent deletion, freeze, cancellation, intake changes and lost ownership. The wave's partial evaluation is explicitly cleared whenever evidence changes, preserving scan correctness that previously depended on the global control revision.
2. Shared wave/policy fences are captured after the member reads. Source-attempt reservations also capture the cleanup-policy guard after remote execution checks, shortening the vulnerable interval without weakening the guard.
3. `WorkStore.release()` supports an explicit, revision-fenced attempt reset for deferred work with verified progress. Vision resets its dispatch budget only after durable wave progress, including pages committed before a later coordination conflict. Consecutive no-progress failures still exhaust the existing five-attempt ceiling. Model/source retry budgets are unchanged.
4. Tag duplicate live claims return an acknowledged no-op without traceback or owner mutation. Early receipts coalesce through one reserved delayed wake.
5. Tag source barriers are checked before acquiring the catalog lease and rechecked after acquisition. A successful durable delayed-wake publication allows the current barrier receipt to be acknowledged; send failures retain it. Wave waits back off from 300 seconds to a maximum of one hour, while completion/cancellation signals resume eligible tag maintenance immediately. Ordinary active-source reservations keep their short retry.

No schema/protocol identifier changes, fabricated completion receipts, failed-member resets or queue purges are part of this fix.

## Validation

The original shared-control commit was loaded from `git show HEAD:backend/wave_source_batch.py` in a separate test process. Real successor `open_session()` operations were interleaved before each policy fence. The original commit exhausted its CAS retries. The patched commit succeeds after the same interleaving without overwriting the successor's control state.

Additional regression cases cover cleanup/freeze rejection, partial-manifest scan invalidation, concurrent terminal settlement, lost ownership, receipt replay without repeat inference, failure-budget reset after progress, preservation across newer revisions, continued exhaustion without progress, duplicate tag ACKs, recovery after owner expiry, failed wake publication, bounded early duplicates and terminal-wave resumption.

- Targeted coordination, maintenance, lease, recovery and cleanup suite: **274 passed**.
- Wave/protocol qualification test selection: **629 passed**.
- These selections overlap; their counts are not additive.
- `git diff --check`: passed.

The wave selection matches the Python tests selected by the image publication workflow, run here in the local tool environment. It is not a published-image qualification or a live post-deployment result.

## Applying and recovering

The fix is local and ready for review; it has not been deployed. Use the existing compatible-wave upgrade process in `deploy/upgrade-wave.md` to publish qualified images and update bindings while preserving source records, checkpoints and receipts. That workflow explicitly requests fresh dispatcher revisions for eligible unfinished releases, which addresses the already-failed `6d61cb0` intent; a normal restart of the old image alone does not repair the cause.

After an authorized rollout/recovery, verify that `6d61cb0` resumes from its checkpoint, reaches a qualified vision freeze, and releases `88397a60` and the subsequent waves. Confirm no new `commit_observations` control-row conflict storm, no repeated duplicate-claim traceback storm, and successful downstream publication. Live throughput and backlog clearance remain unverified while vision is stopped.

## Evidence files

Ignored local directory `.azure/upload-wave-review-20261103/` contains exact KQL and JSON responses:

- `investigation-initial-conflicts*`, `investigation-attempt-errors*`, `investigation-coordination*`;
- `investigation-error-volume*`, `investigation-shutdowns*`, `gap-system*`;
- `investigation-vision-app.json`, `investigation-upload-wave-stage-pending.json`, `investigation-index-model-tag-pending.json`;
- `fix-control-baseline-replay.txt`, `fix-targeted-tests.txt`, `fix-wave-qualification.txt`.

Temporary Azure credential copies used for read-only access were removed at completion.
