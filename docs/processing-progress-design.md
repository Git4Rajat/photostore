# Background processing progress: estimated, durable, push-driven

Status: **implemented** (`cc56618` and the follow-up fixes in `b7cacab`,
`17fd4ea`, `e0cc09c`). This doc keeps the design rationale. The "What exists
now" section and the Rollout section match the code as of 2026-11-24. Where the
code and the original design differ, the difference is recorded below.

Scope: what a person sees between "last photo uploaded" and "everything is
searchable", across the three wave stages: vision, clustering and indexing.

## Goals

1. The upload bar carries on after upload and shows an **estimated** progress.
   One place answers "is it finished yet?".
2. It **survives sessions**: close the tab, reload, or switch from phone to
   laptop, and the same bar comes back with the current numbers. The server owns
   the state, so no browser has to stay open.
3. Updates travel by **Web PubSub on the Free tier** without spending the daily
   quota. Vision, the longest stage, reports in **10% steps**.
4. Polling stays as the fallback, so a dropped or over-budget message only
   delays the bar and never breaks it.

## What exists now

**Wave states** (`backend/upload_waves.py`): `collecting` →
`waiting_predecessor` (when an earlier wave for the library is still running) →
`processing_pending` / `processing` (vision) → `ready` (clustering) →
`indexing` → `complete`, plus `cancelled`.

**Progress module** (`backend/processing_progress.py`):
- Vision counts members as its page commits land. `_report_vision_progress`
  (`backend/upload_wave_execution.py`) records the count next to the page
  checkpoint (`checkpoint('sources', cursor)` in `run_vision_release`). The count
  is of members whose results committed, so retries do not overcount.
- The estimate is 10 steps (`STEPS = 10`). The smoothing factor is 0.3
  (`RATE_ALPHA`). An ETA needs 3 samples (`MIN_SAMPLES`). Before that the bar
  says "Getting started".
- Waves with fewer than 50 members (`SMALL_WAVE`) send only stage changes and
  done.
- ETAs are shown in coarse buckets from 1 minute to 2 hours (`ETA_BUCKETS`).
- A wave that has not moved for 10 minutes (`STALL_SECONDS = 600`) shows "taking
  longer than expected" unless it has failed.
- Progress rows are kept for 24 hours after completion (`RETAIN_SECONDS`), and
  up to 200 rows are returned (`MAX_ROWS`).
- **Cluster and index report no fraction.** They have a state and a start time
  only, so they show as indeterminate segments. The original design allowed a
  counter if one was cheap, and none was added.

**Push** (`backend/index_events.py`):
- `publish_processing(library, wave_id, 'done')` fires from
  `execute_wave_handoffs` (`backend/wave_runtime.py`) when a wave completes.
- `publish_progress(library, wave_id, stage, step, eta_seconds, force=...)`
  sends a step or stage change. The gap per library is
  `INDEX_EVENTS_PROGRESS_MIN_GAP_SECONDS` (default 30 s). Stage changes pass
  `force=True` and skip the gap, but never the daily budget.
- Budgets are per sending process: `INDEX_EVENTS_PROCESSING_DAILY_BUDGET`
  (default 1500) for done messages, and `INDEX_EVENTS_PROGRESS_DAILY_BUDGET`
  (default 3000) for progress messages. Over budget, progress stops and clients
  fall back to polling. Done messages are not dropped by the progress budget.
- `PROCESSING_PROGRESS_PUSH` turns progress pushes off when set to `false`. It is
  on by default.

**Endpoints** (`backend/routes/upload_sessions.py`):
- `GET /api/upload-sessions/<session>/processing` answers the summary for one
  upload session. It returns `{done:false,state}` until the wave completes, then
  `{done:true, processedSeconds, failed, people}`.
- `GET /api/processing` (line 169) returns every active wave for the signed-in
  library, plus waves finished in the last 24 hours that are not yet dismissed.
  This is what makes the bar work after a reload or on a second device.
- `POST /api/processing/<wave>/dismiss` (line 192) hides a finished wave for that
  user on every device.

**Frontend**: `frontend/src/services/processingProgress.ts` (the store that the
bar reads), `frontend/src/services/indexReadyPush.ts` (the Web PubSub socket and
its fallback polling), and `frontend/src/services/indexBuilding.ts` (the shared
"preparing" signal).

## Data model

A progress record sits next to the wave rows in the library's wave partition.
It adds no new table and no extra write on the hot path. Vision writes its count
in the same call as the page checkpoint it already makes.

```
progress = {
  stage:        'vision' | 'cluster' | 'index',
  done:         int,       # members finished in this stage (vision only)
  total:        int,       # wave.memberCount, frozen at stage start
  stageStartedAt, updatedAt: epoch seconds,
  rate:         float,     # members/s, smoothed
  etaSeconds:   int | null # null until enough samples
}
```

Rows are written without compare-and-swap, so they never contend with wave
transitions. They are advisory: the wave row is what decides the state.

## Estimation

- **Per-stage rate**: exponentially smoothed members per second over the last
  few page commits of this wave. Before 3 samples there is no ETA.
- **Overall bar**: each stage is weighted by its expected seconds, then the
  finished fractions are summed. Vision dominates, so the bar mostly moves
  during vision.
- **Never move backwards.** The client clamps the displayed fraction to the
  highest value it has shown for that wave. If reality is slower than the
  estimate, the copy changes to "Taking longer than expected" instead.
- **Coarse time copy**: "about 2 min", "about 10 min", "about an hour", never
  seconds, and "Estimated" in the detail sheet.

*Not built as designed:* the original design seeded the first ETA from the
library's last five completed waves (a `rates!` row). The code uses only the
live samples, so the first ETA waits for 3 samples.

## Push rationing

Free tier: 20 concurrent connections and 20,000 messages per day, across all
senders. The Web PubSub unit stays on Free; paid tiers are not part of the
design. The budgets above keep progress inside that daily cap.

Events per wave, upper bound: vision 10 steps, cluster 2 (start, end), index 2
(start, end), done 1. That is about 15 messages, regardless of photo count.

Rules:

1. **Step, not tick**: a message is sent only when a step boundary is crossed,
   never per page. Crossing several steps in one commit sends the latest one.
2. **Minimum gap**: at most one progress message per library per 30 s, except
   for stage changes.
3. **Daily budgets** per sending process (above). Done messages are never
   budgeted away.
4. **Payload carries data**: `{type:'processing-progress', libraryId, waveId,
   stage, step, etaSeconds}`. The client can draw from it without a read. The
   `GET /api/processing` response stays authoritative, and the push is advisory.

**Fallback**: socket connected → one safety GET every 2 minutes. Socket down or
over budget → GET every 20 seconds while the bar is visible and the tab is in
the foreground, nothing in the background.

## Frontend

- `ProcessingProgressStore` (`processingProgress.ts`) is fed by the push and by
  `GET /api/processing`. It exposes the stage, the fraction, the ETA label, the
  steps and whether the wave is done.
- The bar shows the current stage's segment with the vision fraction when known,
  and a sliding indeterminate animation otherwise. The detail sheet adds
  "Estimated, about N min left".
- Finished steps keep their result (for example "9 people found") from the
  existing summary.

## Notifications

The bar carries progress. The Notifications list gets exactly one entry per
wave: a "done" entry. 10% steps never create notification entries or toasts. A
stalled wave adds one "taking longer than expected" entry, replaced by the done
entry later, so the count stays at one per wave.

## Rollout status

1. **Done.** Backend writes progress with the vision page checkpoint. Adds
   `GET /api/processing` and dismiss. Clients that do not call them are
   unaffected.
2. **Done.** Progress pushes are on by default, with the budgets and minimum gap.
   Sends and skips are not logged yet; add diagnostics events if the budget
   needs tuning.
3. **Done.** The frontend store and bar are in `processingProgress.ts`. Polling
   stays as the fallback.
4. **Open.** Cluster and index fractions. They need a cheap counter in their
   release loops. Without one they stay indeterminate.

Deploy only when no upload is running. A rollout aborts browser sessions.

## Open questions

- **Quota accounting.** Confirm that a group message counts once per receiving
  connection. If it does not, the budget is looser than stated.
- **Live push.** The 2026-11-18 note recorded that the browser socket connects
  only after index activity, and a test upload showed no delivered messages. The
  cause was not recorded. Verify on the current deployment, because the design
  depends on the socket.
- **Mockup.** The design mock-up shows no percentage. It should show the
  estimated state: a fraction on the current segment, "about N min left", and
  "taking longer than expected".
- **Wave merging.** Several uploads can share one wave. The bar then shows the
  wave's total, not the user's own photos. Decide whether that copy is
  acceptable.
- **Skip publish when nobody is watching.** A connection-count check costs a
  REST call. Only worth it if the budget proves tight.
