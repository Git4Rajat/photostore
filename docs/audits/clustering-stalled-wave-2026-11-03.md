# Clustering stalled wave — November 3, 2026

Read-only Azure investigation at approximately 16:00–16:05 UTC. Affected wave: 88397a60bdca46d1aa61aebccf6e4711, 9,980 photo/revision members. No application code, cloud configuration, queues, or Table rows changed.

## Finding

Revision rollouts interrupted long-running clustering. The queue wrapper then exhausted its delivery-count budget, including deliveries that only encountered an existing dispatcher lease. The sixth delivery was dead-lettered before stage dispatch. The durable stage remains unfinished, but both runnable cluster queues are empty, so there is no delivery to resume it.

This is a deployment-interruption and recovery defect. The observed WorkLeased exception is an intermediate contention symptom, not proof that a live owner still holds the work now.

## Timeline

All times below are Stockholm (UTC+1) on November 3; raw evidence uses UTC.

| Time | Evidence |
|---|---|
| 14:39:07 | Revision 66 starts stage, dispatcher attempt 1, queue dequeue 1. |
| 14:39:55 | Revision 66 receives SIGTERM during revision deactivation. |
| 14:51:53 | Revision 67 receives dequeue 2, fails with WorkLeased. |
| 14:53:53 | Revision 67 starts stage, dispatcher attempt 2, dequeue 3. |
| 15:37:06 | Revision 67 receives SIGTERM during rollout to revision 68. |
| 15:48:41 | Revision 68 receives dequeue 4, fails with WorkLeased. |
| 15:50:43 | Revision 68 starts stage, dispatcher attempt 3, dequeue 5. |
| 16:22:59 | Revision 68 receives SIGTERM during rollout to revision 69. |
| 16:32:21 | Last durable checkpoint: faces-applied:8576. |
| 16:32:37 | Old revision still reports active processing; last observed RSS approximately 273 MiB. |
| 16:34:50 | Revision 69 dead-letters dequeue 6 because maxRetries is 5. |
| 16:35:24 | Revision 69 scales to zero after the runnable queue empties. |

The configured terminationGracePeriodSeconds is 600. Old-revision telemetry ends shortly before that ten-minute window expires after SIGTERM, without a stage completion or exception summary. That supports forced shutdown at the grace limit; there is no explicit SIGKILL event in the queried system logs. The logs show revision deactivation and stopping containers, not an OOM diagnosis.

## Durable and queue state

- Wave remains ready: Vision is complete, assignment/index publication is not.
- Execution status remains running, epoch 3, checkpoint faces-applied:8576, but leaseUntil was 15:35:45 UTC and is expired.
- Dispatcher attempts=3, failed=false; its leaseUntil was 15:35:44 UTC and is expired.
- photostore-clustering: approximate count 0, peek empty.
- photostore-clustering-deferred: approximate count 0, peek empty.
- photostore-clustering-deadletter: count 1. Peek identifies this exact wave/stage/scope/intentRevision and records dequeueCount=6, maxRetries=5.
- No assignment manifest at assignment!<wave>!manifest was found. This absence alone is not a full journal-integrity assessment; assignment journals use their own names and may live on the checkpoint mount.
- Latest cluster revision 69 reports Running/Succeeded at app level, minReplicas=0, maxReplicas=1. Main queue scaling uses queueLengthStrategy=all. App readiness does not imply this wave is progressing.

## Code mechanism

backend/wave_runtime.py calls work.claim(token) before entering its stage execution try block. WorkStore.claim raises WorkLeased while leaseUntil is in the future. That propagates to the queue wrapper, which leaves the message for retry.

backend/app.py:_process_clustering_queue_message_impl checks dequeue_count > max_retries before dispatching. Only validated durable index-build messages receive the exemption that relies on ledger claims. upload_wave_stage messages do not. Thus six queue deliveries can exhaust a wave with only three actual dispatcher claims; two observed deliveries did no clustering because of lease contention.

The cluster SIGTERM handler stops new intake but lets the entire current job keep running. It does not yield at a bounded assignment checkpoint before the 600-second shutdown deadline. The old process continues lease renewal while draining. A replacement delivery can therefore arrive after queue visibility expires but before the longer dispatcher lease expires and hit WorkLeased. After the final interruption, the sixth queue receive is rejected outright rather than consulting recoverable durable work.

## Recovery and permanent fix

Immediate recovery: once no live owner remains and leases are expired, requeue the exact preserved stage payload and let a fresh owner resume through the existing assignment journal. Do not reset source processing, clear assignment journals, fabricate receipts, or mark the wave complete. Stage dispatcher has only three actual claims, so this is distinct from its five-attempt failure ceiling. Verify the mounted journal/checkpoint recovery path during resume.

Permanent changes:

1. Treat valid wave-stage delivery as a durable wake. Use the authoritative ledger's actual failure attempts rather than raw queue dequeue count for exhaustion.
2. Handle WorkLeased as expected deferral, with a durable replacement wake after the active lease can expire; do not merely ACK without preserving recovery.
3. Yield long clustering work at journal-safe boundaries on shutdown/deadline, releasing ownership and durably scheduling continuation before grace expires.
4. Recover eligible expired stage work that has neither a live owner nor an outstanding wake. Keep this recovery bounded and idempotent.

Evidence under .azure/upload-wave-review-20261103: cluster-live-records-1600.json, cluster883-timeline-1600.json/.kql, cluster883-errors-restarts-1600.json/.kql, cluster883-system-1600.json/.kql, cluster-old-final-logs-1600.json/.kql, cluster-renewal-errors-1600.json/.kql, cluster-delivery-lifecycle-1600.json/.kql. Queue reads used peek, not receive.

## Remediation — November 3, approximately 16:07 UTC

Implemented locally:

- A role-matching wave-stage wake bypasses the generic queue dequeue ceiling only when its library, scope, and intent revision match a persisted stage intent. The existing durable claim-attempt ceiling still applies. Invalid or forged wakes retain generic dead-letter handling.
- WorkLeased publishes a delayed replacement delivery before acknowledging the original. It leaves the current owner and attempt count unchanged. A failed replacement send propagates so the original message survives.
- Clustering uses a default 120-second slice budget, checked at journal-safe page boundaries after new assignments are durable. A yielded slice stops execution keepalive, releases its ownership, and returns to normal durable continuation scheduling. The final page completes publication normally. Replay of an existing prefix cannot consume the entire slice without making new durable progress; preparation and individual pages are not hard time-limited.

Verification: 625 tests passed across wave and upload-wave suites, execution/dispatcher leases, clustering retries, dead-letter handling, and canonical/local journal recovery. Added tests prove bounded slices reach the same canonical assignment as an uninterrupted pass, do not reapply faces, release ownership without publishing a premature assignment receipt, preserve delivery on failed replacement sends, and reject forged retry exemptions. git diff --check passed.

Live recovery: both leases were rechecked expired, wave remained ready, actual dispatcher attempts were three, and main/deferred cluster queues were empty. Replayed the exact preserved originalPayload to photostore-clustering once. New message ID: ea4a2338-374f-4ebd-82f6-d10d226378c3. The original dead-letter entry remains for audit. No source states, assignment journals, receipts, frozen inputs, or lease rows were reset. Private mutation evidence: .azure/upload-wave-review-20261103/cluster-recovery-20261103.json.

The live recovery uses the currently deployed image. The permanent code changes have not been built into or rolled out as a new container image by this remediation.

Recovery verification: the deployed worker emitted wave_stage_started at 16:08:24 UTC, acquired execution epoch 4 and dispatcher attempt 4, and held fresh unexpired execution and dispatcher leases. No journal restoration error was present in the queried logs at 16:09 UTC. This verifies reclamation; it does not certify full wave completion or deployment of the new local fixes.
