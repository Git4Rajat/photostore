# Upload lifecycle recovery on Core traffic

Implemented source changes; these do not change a running Azure deployment.

- New table: `photouploadsessions`, configurable with `UPLOAD_SESSIONS_TABLE`.
  Library partitions contain `session!`, `notice!`, survey and migration rows.
- Upload indexes live intake before opening the authoritative wave session.
  Existing heartbeat writes remain in the wave-session table; the directory
  receives no per-file or heartbeat activity writes.
- Core checks on authenticated user traffic, throttled per library/process.
  The browser explicitly reconciles on foreground return, reconnect and
  throttled user input. Pending cleanup receives at most ten foreground
  follow-ups at 30-second intervals, then resumes on later user activity.
- Staleness uses the existing 300-second server deadline. Registration,
  resolution and source admission piggyback activity on existing session
  mutations. Core conditionally claims the directory entry, rechecks/fences
  the authoritative session, respects the 30-second finalization grace, and
  drains up to 400 reservations per pass. Finalized sources are preserved;
  only those sources continue to downstream processing.
- Existing stage handoffs/outboxes remain in use. No new queue, worker or
  sidecar is introduced. The expiry producer, clustering expiry poller, queue
  resource and KEDA expiry rule have been retired in source/templates.
- Core atomically publishes the stale-closure notice and removes its session
  directory entry after handoff. Failure retains the directory. Notice/read
  receipts expire after 14 days; bounded retention cleanup runs on user visits.
- Bell notices appear outside Tools and offer Retry unfinished files. Retry
  targets the matching local checkpoint and creates a linked successor. If
  that checkpoint/files are absent, the browser asks the user to reselect.
- One-time Core adoption surveys only open historical sessions in bounded
  pages. It retains a durable cursor, and cannot reopen or resurrect old waves.

## Storage overhead

Normal live admission adds one directory create. Terminal settlement adds a
point read and conditional delete. Heartbeats add zero calls to the new table.
An ordinary stale path adds a directory scan/claim, an atomic notice-and-delete
transaction, notice reads and acknowledgment. Shared cursor/migration reads,
paging, conflicts and interrupted attempts mean ten calls is a planning budget,
not a hard upper bound. Existing wave, metadata, blob and stage-handoff calls
are separate from these additional lifecycle-directory calls.

## Operations

Structured events `upload_session_reconciled` (stale, cancelled) and
`upload_session_reconcile_deferred` (reason) expose completed cleanup and
concurrency/finalization deferrals. Storage/relay failures are logged as
`Upload session reconciliation unavailable` or
`User-triggered upload recovery deferred`. These use existing application logs;
no additional monitoring service or dashboard resource is provisioned.

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h)
| where Log_s has_any ('upload_session_reconciled',
                      'upload_session_reconcile_deferred',
                      'Upload session reconciliation unavailable',
                      'User-triggered upload recovery deferred')
| summarize Events=count() by ContainerAppName_s, bin(TimeGenerated, 1h)
```

## Deployment notes

Deploy Core, Upload, browser assets and clustering configuration together using
the existing wave capability rollout. Until deployment, existing expiry scalers
and consumers remain active. The retired cloud queue may retain old messages;
new code does not consume it, and the updated scaler no longer wakes clustering
for it. No live queue was purged as part of this code change.

Bicep validates with the installed compiler. The committed ARM snapshot had
unrelated drift from its Bicep source; its edit is scoped to replacing the expiry
queue resources/dependencies and deleting the expiry scaling rule, preserving
existing unrelated runtime settings. Stale generator hashes were removed from
the edited ARM snapshot.
