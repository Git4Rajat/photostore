# Queue-driven wave lifecycle fact check

Date: 3 November 2026. Scope: current local source and primary documentation;
no live cloud inspection, deployment, queue mutation or application change.

## Conclusion

**Updated constraint:** retain Container Apps for all existing services. The Jobs
option below is historical analysis and is not the selected direction. See the
Container Apps design addendum at the end.

Feasible with Azure Storage Queues. A stage message can remain undeleted while
the stage runs, reappear after a crash, and be deleted only after durable stage
completion and successful downstream publication. This supports arbitrarily
long periods without worker instances. It does not provide exactly-once delivery;
durable wave identity, ownership fencing, checkpoints and idempotent writes remain
necessary. No design can be certified perfect by source review alone.

## Documentation checked

- [Microsoft: Update Message](https://learn.microsoft.com/en-us/rest/api/storageservices/update-message):
  invisibility can be continually extended; each update returns a fresh pop
  receipt. Visibility expiry permits another receive. Use the latest receipt.
- [Microsoft: Put Message](https://learn.microsoft.com/en-us/rest/api/storageservices/put-message):
  default message lifetime is seven days; `messagettl=-1` permits no expiration.
  Visibility delay and message lifetime are different settings.
- [Microsoft: queue comparison](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-azure-and-service-bus-queues-compared-contrasted):
  Storage Queue delivery is at least once and cross-service transactions are
  not supplied by Storage Queues.
- [Microsoft: Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app):
  external KEDA scaling supports zero replicas; multiple rules can independently
  activate the app. Queue polling is performed by the scaler.
- [Microsoft: event-driven jobs tutorial](https://learn.microsoft.com/en-us/azure/container-apps/tutorial-event-driven-jobs):
  the application receives/processes/deletes the queue message; Microsoft
  explicitly says to delete only after processing finishes.
- [Microsoft: Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs):
  event-triggered executions suit finite, long-running work. Execution timeout,
  platform retries and queue retries must be configured together.
- [KEDA 2.18: Azure Queue scaler](https://keda.sh/docs/2.18/scalers/azure-storage-queue/):
  `all` counts invisible messages; `visibleonly` peeks visible messages and falls
  back to `all` at 32 visible messages. This version documents semantics, not
  verification of the managed KEDA version in the live environment.

## Existing building blocks and gaps

1. `backend/wave_runtime.py:37` already sends library, wave, stage and intent
   revision. It omits an explicit message lifetime, so stage messages currently
   inherit the queue client's/service default unless configured elsewhere.
2. `backend/app.py:_process_clustering_queue_message_impl` renews message
   visibility and retains fresh pop receipts. Uncaught failures preserve the
   message. A normal handler return permits deletion regardless of whether
   that return represents completed work, a continuation or a no-op.
3. `backend/wave_runtime.py:consume` releases incomplete work, sends a replacement
   wake and returns normally. Artifact-cleanup waits also use replacement wakes.
   The current contract is advisory wakeups plus a durable ledger, rather than
   one retained message per stage until completion.
4. `backend/upload_waves.py:commit_assignment` atomically records assignment
   completion and the indexing outbox in one Table partition. Execution leases
   in `backend/execution_lease.py` fence old owners; publication receipts verify
   exact wave obligations. Preserve these mechanisms.
5. A source-level handoff risk exists: after `commit_assignment` changes the wave
   to `indexing`, a crash before `relay_current` sends the index wake leaves a
   durable outbox but no guaranteed queue delivery. Redelivered cluster messages
   fail `outbox_eligible` and return `noop` before repairing that handoff. This
   is a source-path finding, not an observed live incident or an injected-crash
   reproduction. Upstream completion/replay must drain pending downstream
   handoffs before acknowledging obsolete/completed upstream receipts.
6. `WorkStore.notify` reserves `wakeUntil` before sending. A reservation, or a
   false return because a reservation exists, is not proof that Azure accepted
   a message. Source acknowledgment must not rely on that alone.
7. `backend/wave_recovery.py` and the final comment in `deploy/resources.bicep`
   describe event-triggered nudges and operator sweeps, with no standing
   scheduled recovery host. A durable outbox alone therefore does not guarantee
   autonomous recovery when no further events occur for months.
8. Main queues use `all`, protecting running work but also counting delayed
   retries. Deferred queues use `visibleonly`, allowing delay-time sleep but
   hiding claimed work from their scale signal. Renewing visibility does not
   itself guarantee a Container App will remain running. A simple change to
   retain and delay the same message is insufficient for both requirements.
9. Upload-expiry and library operations independently wake cluster; indexer's
   HTTP API shares a replica with its worker. These triggers must be separated
   if heavy wave compute is to start only for eligible stage work.

## Recommended implementation contract

- Use one logical message per `(library incarnation, wave, stage, input revision)`.
  Duplicate physical deliveries remain valid. Resolve all required processing
  outcomes before releasing clustering; commit assignment before releasing index.
- Explicitly use nonexpiring stage and dead-letter messages. Validate payloads
  against authoritative frozen inputs; preserve cancellation/supersession checks.
- Keep the stage receipt until stage completion AND accepted downstream enqueue.
  On replay after completion, repair the downstream handoff before deleting.
  Never consider a send reservation equivalent to successful publication.
- Renew queue visibility during useful work, retain execution fencing, and stop
  mutations after authoritative ownership loss. Checkpoint bounded work; a hang
  must eventually hit a timeout rather than renew forever.
- Failed attempts use increasing delays with jitter and a durable failure budget.
  Healthy checkpoints and duplicate deliveries must not consume that budget.
  Persistent failures enter a preserved, visible terminal failure with an alert
  and explicit replay path. Do not automatically resurrect them indefinitely.
- Evaluate dedicated event-driven Container Apps Jobs as the preferred fit for
  rare finite waves: no API sidecar, minimum executions zero, bounded execution
  concurrency, finite timeout, and a one-message entrypoint that exits. A
  visible-only trigger can avoid starting jobs for delayed messages; an already
  started job has an execution lifecycle independent of app replica scale-down.
  Account for duplicate executions using durable stage ownership. Validate the
  managed scaler/version and backlog behavior in Azure before selection.
- Existing Container Apps can remain an alternative, but require an explicit
  active-work signal and retry scheduling design that both protects in-flight
  work and permits sleep during backoff. Do not claim `all` or `visibleonly`
  alone satisfies both.
- Move upload abandonment/expiry to lightweight event-triggered work. Ensure the
  first accepted upload has a durable queued recovery anchor before returning
  acceptance. Retaining downstream predecessors does not repair a missing first
  enqueue. Preserve all-duplicate, zero-face and cancelled-wave stage skipping.

## Verification

132 existing focused tests passed across wave runtime, stage consumers, execution
leases, recovery, clustering retry/visibility and index wake receipt handling:

```bash
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave_runtime.py \
  backend/tests/test_upload_wave_execution.py \
  backend/tests/test_wave_execution_consumers.py \
  backend/tests/test_execution_lease.py \
  backend/tests/test_wave_recovery.py \
  backend/tests/test_clustering_worker_max_retries.py \
  backend/tests/test_clustering_worker_visibility_timeout.py \
  backend/tests/test_index_wakeup_receipts.py -q
```

These validate existing behavior, including continuations, not the new contract.
Release gates: inject termination before/after stage commit, enqueue acceptance
and acknowledgment; simulate accepted-send/lost-response, renewal loss, duplicates,
long stages, poison failures and delayed retries. Verify correct receipts and
publication, no new upload needed for recovery, zero workers with empty queues,
and zero workers throughout delayed retry waits. Use a simulated long clock gap
for retention tests and a measured Azure idle/start/retry run; local mocks cannot
certify scaler or platform behavior.

## Container Apps design addendum: waves and user actions

Keep existing apps and API behavior. Adopt explicit worker outcomes rather than
equating a normal Python return with successful work. Complete, safely obsolete,
cancelled and preserved-terminal-failure outcomes permit acknowledgment only
after their respective durable evidence is recorded; retry/incomplete outcomes
retain the source message and update its visibility delay. Invalid messages
must be preserved in a dead-letter queue with a reason, rather than silently
counted as completed.

Wave messages identify library incarnation, wave, stage and immutable input
revision. User-triggered messages instead identify a durable operation ID,
library, action and captured parameters/revision; a wave ID is unnecessary for
actions outside a wave. Both use the same acknowledgment/retry rules. Preserve
the scope-based index ledger and existing inline edit paths; newer revisions
arriving during a build remain pending until covered. A completion badge is
not sufficient proof of index publication.

In `app.py`, at least the single-person identity-propagation branch catches an
exception, marks failure and returns normally. Under the generic consumer this
permits deletion. Audit each user-action handler to distinguish a retriable
failure from an explicitly recorded terminal rejection. Repeated user actions
and wave retries must respect authoritative curation revisions and existing
catalog serialization. The legacy Blob lease in `clustering_lease.py` explicitly
does not fence Table writes; do not describe it as a cross-service transaction.

Retain the existing atomic completion/outbox records, but replay completed
upstream stages through handoff repair before acknowledging their messages.
Queue acceptance, not merely a durable outbox or `wakeUntil` reservation, is the
required downstream handoff condition. An accepted-send/lost-response may
produce a duplicate; identity and durable completion make that safe.

**Scaling design to qualify:** use visible-only scaling on work queues so an
invisible delayed retry does not itself demand a worker. Add an independent,
expiring active-work signal to keep a replica while it processes a hidden
message. One implementation using existing Azure Queue capabilities is a
separate activity queue per app: while actually executing, publish short-lived
activity tokens periodically; scale on that queue with `all`, without consuming
the tokens. Old tokens expire automatically. Stop publishing on retry, finish,
ownership loss or no-progress timeout. A crash therefore leaves only a bounded
TTL/cooldown tail, rather than permanent active demand. This queue contains
ephemeral activity signals, never the only copy of business work.

This activity-queue proposal is engineering inference from documented TTL and
queue scaler semantics, not a Microsoft-prescribed end-to-end recipe. Heartbeat,
TTL and cooldown margins must accommodate scaler polling, approximate queue
counts and storage latency. Verify expiry-driven scale-down on the deployed
managed scaler, including visible-only fallback at 32 messages. No chosen timing
values or cloud residency guarantees are asserted by this audit. A supported
custom active-work metric is an alternative, subject to its own stale-signal
and delivery-latency checks.

Queue visibility renewal, activity heartbeat and durable stage ownership are
separate responsibilities. Renewing visibility alone never establishes worker
residency or exclusive authority to mutate data. All three must cease or fail
closed on loss of authoritative ownership. Preserve the original stage message
while processing, while checkpointed but incomplete, and during delayed retry.
Use a durable failure budget rather than dequeue count alone. Persistent errors
must reach a preserved explicit failure and alert instead of consuming compute
forever.

Other scale rules remain independent: indexer HTTP requests can still start its
sidecar, and upload-expiry/library operations can still start cluster. Either
accept that residency for requested work, reduce allocations, or separately
address those responsibilities; changing wave acknowledgments does not remove
these triggers. No app-to-Job migration is required or selected.
