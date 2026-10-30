# Cluster false-start wake-ups: root cause and fix (2026-10-30)

LAW analysis + code trace, no local benchmark needed (the mechanism was
confirmed by reading the actual send path, not inferred from timing alone).

## The finding

Over a ~19h window, 5 of 12 cluster replica lifetimes did **zero** clustering
work (processing_seconds < 0.15s) yet each lived 173-276s before scaling
down. Every replica that did real work still idled 2-5x its own processing
time afterward. Of 4,481s total billed replica-time: only 25% was useful
work, 24% was pure false starts, 50% was idle cooldown tail after real work
finished.

Root cause, traced through the code (not inferred): every wave-continuation
retry (`progress`, `barrier_wait`, `artifact_cleanup` 30s, `wave_active` 60s)
goes through `index_work.WorkStore.release(..., retry_delay=...)` ->
`WorkStore.notify()` -> `wave_runtime.send_stage(..., delay=retry_delay)` ->
`queue.send_message(..., visibility_timeout=delay)` on the **same**
`photostore-clustering` queue KEDA watches with `queueLengthStrategy: 'all'`.
`'all'` counts invisible messages regardless of *why* they're invisible --
including ones invisible only because they're deliberately scheduled for
later, not because a live worker is holding them. So every delayed retry
keeps KEDA's queue-length metric >= 1 for its whole delay window, waking or
holding open a 2vCPU/4Gi replica that finds nothing to do until it's due.

`'all'` can't simply be switched to `'visibleonly'`: it was chosen
deliberately (see the `clustering-queue` scale rule comment in
`deploy/resources.bicep`) so KEDA doesn't see an **actively-renewed in-flight
job's** message as idle and SIGTERM-kill the replica mid-run (a real prior
incident, `clustering-worker-sigterm-job-loss`). The two cases -- "a live
worker is renewing this message because it's actively processing it" vs.
"this message is invisible because it's intentionally delayed" -- look
identical to KEDA on one queue.

## Fix

Split them onto two queues. Delayed continuations (`delay > 0`) route to a
new sibling queue, `photostore-clustering-deferred`, scaled with
`queueLengthStrategy: 'visibleonly'` -- correct for messages that are never
actively held, only ever waiting out a schedule. The original
`photostore-clustering` queue keeps `'all'`, still protecting genuinely
in-flight jobs. `send_stage()` in `wave_runtime.py` picks the queue by
`delay`; `run_clustering_worker`'s poll loop drains the new queue as a third
priority tier, reusing the existing dispatch and dead-letter queue unchanged
(same payload shape, `type=upload_wave_stage`).

Scoped to the cluster stage only. Vision (`photostore-ipwork`) and index
(`photostore-index-maintenance`) have the identical `'all'`-strategy pattern
and the identical false-start exposure; left as a follow-up using this same
shape.

## Projected impact

Not yet measured post-fix (the code shipped alongside the queue/scale-rule
infra in this change, but no wave has exercised the deferred queue yet at
time of writing). Expected: eliminates the false-start share of billed time
entirely (~24%) and removes the cooldown-tail inflation specifically caused
by delayed retries (part of the 50% idle-tail share; some idle tail is
inherent to the 300s `cooldownPeriod` regardless of this fix and is a
separate, un-shipped lever).
