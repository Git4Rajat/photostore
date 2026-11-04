# Post-wave maintenance barriers — November 4, 2026

Read-only investigation at approximately 09:54–10:00 Stockholm. No cloud
configuration, source records, queues, reservations, or application code changed.

## Finding

All persisted waves in both libraries are terminal, but hack4heart's automatic
artifact coverage still records active source writes. These barriers prevent
maintenance from publishing vector, tag-embedding, and lexical artifacts.
Repeated barrier retries explain the remaining Vision replica and recurring
Indexer activation. This is distinct from unfinished wave clustering.

| Artifact | Active reservations | Control last modified (Stockholm) |
| --- | ---: | --- |
| vector | 1 | November 4, 04:35:38 |
| tag_embedding | 2 | November 4, 04:35:38 |
| lexical_artifact | 7 | November 4, 06:25:26 |

Each affected control has `fullRequired=true`, an empty base, and an empty
covered revision. Hash and people-embedding controls have active=0 and matching
covered/current revisions. There is no operator-quiescence row. The three
affected revisions remained unchanged between the investigation's reads.

## Mechanism and live evidence

1. `SourceTable` wraps source writes with `CoverageStore.begin()` and
   `CoverageStore.finish()`. Begin increments an aggregate active counter;
   finish decrements it. The design deliberately does not expire reservations:
   an old writer may be paused rather than dead. Process termination, failed
   finalization, or partial multi-page begin can leave a reservation behind.
2. `CoverageStore.capture()` rejects active controls with `SourceBusy`.
   Tag maintenance also checks its active control before acquiring the catalog
   lock. A completed wave does not clear these independent source barriers.
3. The live tag intent has `retryReason=barrier_wait`, a 10-second retry delay,
   attempts=0, no execution owner, and no active lease. Its delayed wake is on
   `photostore-ipwork`. The Vision scaler uses `queueLengthStrategy=all`,
   queueLength=1, minReplicas=0, and cooldownPeriod=300. The queue contained
   one message; delayed messages count toward demand. A successful deferral
   replaces the wake before ACK, sustaining demand despite no photo inference.
4. Vector and lexical intents also have `retryReason=barrier_wait`, attempts=0,
   no owner, and no active lease. Their delays are 300 seconds. The immediate
   index-maintenance queue was empty; the deferred queue contained two messages.
   The deferred scaler uses visible-only demand, so it can scale down between
   retries but wakes again when the blocked work becomes visible.
5. Barrier deferrals refund attempts. These waits therefore never exhaust the
   poison-work budget; retry accounting is behaving as implemented.

Latest sampled Vision telemetry reported idle with approximately 3h11m of idle
time over a 3h14m process lifetime. Its logs repeatedly touch the same tag intent
roughly every ten seconds. Indexer telemetry also reported idle following brief
maintenance processing. Two older failed intents for synthetic library names
were present, but have failed=true and are not the runnable intents above.

## What is established and what remains unknown

The current blocking records and retry/scaling chain are directly verified.
Their age and lack of progress strongly suggest abandoned source reservations.
The current ledger contains aggregate counts without per-reservation owner,
operation identity, or start timestamp. Its modification timestamp is the
latest control mutation, not the reservation's creation time. It cannot identify
which specific source operation left each increment behind.

A two-day query found no retained `Automatic source finish failed`,
`Automatic source finalization failed`, or `Unbalanced source reservation`
messages. That does not exclude forced termination, missing telemetry, earlier
failures, or failures without those strings. Earlier audits document coverage
CAS/finalization failures, but do not prove the origin of these exact counts.

## Remediation

Use the existing operator fence/prove/recover/resume workflow after explicitly
stopping and draining all library writers, including paused requests. It
preserves dirty IDs, persists rebuild intents, resets orphan counters under the
catalog lock, and requires full rebuilding. Do not simply clear active counters
or delete wake messages: that can hide a resumed writer or strand dirty work.
This investigation did not perform recovery or claim an all-writers-stopped proof.

Address the independent scaling defect by routing delayed tag maintenance to a
deferred queue with visible-only scaling while retaining in-flight protection
for real Vision work. Longer or event-driven barrier waits can reduce futile
retries, but cannot repair an abandoned counter. Add per-operation reservation
identity and diagnostics to make future attribution and recovery reviewable.
Verify successful artifact certification, intent/queue drain, and all replicas
returning to zero after recovery; wave completion alone is insufficient.

## Relevant code

- `backend/automatic_index_updates.py`: `SourceTable`, `CoverageStore.begin`,
  `finish`, `capture`, `recover_quiesced_source_reservations`.
- `backend/app.py`: `_send_tag_index_wakeup`,
  `_run_tag_index_maintenance_payload`, `_run_index_maintenance_payload`.
- `backend/index_work.py`: `notify`, `claim`, `release`.
- `deploy/resources.bicep`: Vision all-message scaler and Indexer deferred scaler.

## Remediation in this session

The user authorized repair. After coordinating with a separate deployment of
`216f718`, all eight app roles were stopped and verified without serving/worker
replicas. A temporary non-serving core revision used the existing managed
identity to acquire the actual catalog lock and run the existing operator
prove/recover/resume procedure. It completed at **10:26:13 Stockholm**. Counts
changed vector 1→0, tags 2→0, lexical 7→0; the other two controls remained zero.
All five full rebuild intents were persisted, dirty IDs retained, and the fence
was removed. The temporary command, probes and replica floor were then restored.
No originals, wave memberships, source retry counts or completed receipts were
removed or reset.

Permanent fix is commit `61a32ca` on `codex/post-wave-maintenance-216f`, based on
`216f718` to preserve that separately deployed work. Delayed tag maintenance now
uses `photostore-ipwork-deferred` with visible-only scaling. The worker forwards
due messages to the protected main queue before ACK, retaining the original on
failed transfer. Stored wake receipts allow cancellation after successful work.
Source-barrier waits use the same five-minute recovery interval as Indexer.

[Image qualification run](https://github.com/Git4Rajat/photostore/actions/runs/41806993971)
passed **775 backend and 262 Vision tests** in the exact published images.
The standalone Bicep source compiled successfully with existing diagnostics.
Both library bindings migrated successfully at approximately 10:45 Stockholm
through the existing same-protocol migration. This preserved the actual
persisted previous certificate rather than assuming it matched a recently
changed image tag. The upgrade ID is `7a51545052ae4e898cedbba36e33183b`. Private phase/snapshot and
verification evidence is in `.azure/maintenance-recovery-20261104/`.
