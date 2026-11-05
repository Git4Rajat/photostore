# Upload interruptions release processing waves

An upload selection has a renewable server lease. Losing the browser pauses
transfer; it does not hold already admitted files indefinitely. A returning
browser continues unfinished files through a fresh linked intake. Repeated
interruptions repeat this boundary without reopening an old processing wave.

## Timing and lifecycle

- The active upload sends the existing transfer heartbeat with `waveSessionId`
  every 90 seconds, including registration, hashing, transfer and finalization.
- A session lease lasts 300 seconds after its last successful renewal. A missing
  heartbeat is treated as inactivity, whether caused by sleep, browser closure,
  lost connectivity or browser timer suspension. Closing a tab need not deliver
  a final HTTP request.
- A delayed `photostore-upload-expiry` queue check exists before admission can
  commit. Failure to send that initial check fails admission; it cannot create
  an intake that depends on the browser returning. Heartbeats update the session
  deadline, not the queue. A check rereads the deadline and reschedules itself.
- Expiry atomically fences the parent and detaches its wave from new intake.
  Renewal and expiry race on the same session ETag. Expiry cannot be renewed.
- Accepted and duplicate outcomes remain intact. Pending reservations are
  cancelled in atomic 20-item pages, up to 20 pages per delivery, with a durable
  continuation when more remain. Unregistered declared entries do not have to
  be manufactured or falsely counted as uploaded.
- Existing source finalizations retain their bounded leases (currently up to
  600 seconds). Expiry retries after 30 seconds while a scanned page contains
  one. An already running finalization can still admit its source before
  closure. New init/finalize attempts cannot enter an expired reservation.
- Closing uses the normal wave barrier and durable Vision handoff. Another
  active parent sharing that wave must also close before the wave is released.
  Subsequent stages retain their existing predecessor publication requirements.
- Actual start time also includes queue polling, worker cold start, cleanup
  pages and any existing processing predecessor. Five minutes is an inactivity
  threshold, not a guaranteed deadline for Vision to start.

## Resuming

Opening the old parent is an authoritative status read; expired manifests are
not replayed. The browser creates a linked parent for unfinished local files,
retains blob/cache keys, changes admission item IDs, and saves the new child
binding. Transfer lanes share that transition and copies held by automatic retry
receive the new item binding. Completed local files keep their original parent.
Server-relative lease durations avoid depending on the browser's wall clock.
A lost completion acknowledgment can still require normal duplicate reconciliation.

Each later interruption expires the latest parent in the same way. The existing
upload session/cache remains resumable; expiry does not delete those browser
checkpoints. Available cached files/handles are still required to resume bytes.

## Worker and rollout

The cluster app consumes the expiry queue in an independent lightweight thread;
a long clustering job cannot block intake expiry. Existing success-only queue
acknowledgment and lease renewal apply. Transient failures retain the check for
redelivery, and a redelivery after closure repairs a failed stage handoff.

Bicep and the generated ARM template provision the queue and an independent
`visibleonly` KEDA rule. Delayed deadlines do not hold a replica awake; due checks
can wake the app from zero. Normal stage queues continue counting claimed work.

Deploy the queue/scaler and updated cluster worker before enabling the updated
upload API and frontend. Updating images alone does not install the new scaler.
Existing unleased sessions acquire a lease on their first new session-specific
heartbeat; old clients do not become session-aware automatically.

## Evidence and validation

`heartbeatAt`, `leaseExpiresAt` and `leaseExpiredAt` are durable per-session
observations. `upload_session_expiry_deferred` and `upload_session_expired` report
finalization delays, cleanup counts and expiry lag. Generic queue start/failure/
acknowledgment telemetry covers delivery and storage failures.

Backend tests cover four interruption/resume cycles, independent browser leases,
ETag races, source binding fencing, late finalization preservation, partial
registration, bounded cleanup continuation, handoff replay and independent
polling. Flask tests verify authenticated renewal and admission refusal when the
expiry wake cannot be stored. Browser tests cover repeated expiry, concurrent
lanes, restored closed intake, retry copies and library changes.

Validation: 506 backend tests and 114 browser tests passed; TypeScript checking
and Bicep compilation passed. Infrastructure changes are prepared locally and
have not been deployed.
