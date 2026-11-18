# Background processing progress: estimated, durable, push-driven

Status: design, nothing built. Scope: what a person sees between "last photo uploaded"
and "everything is searchable", across the three wave stages (vision, clustering,
indexing). Visual source: the "After the upload: getting photos ready" section of
`docs/design-mockups/keepsake-redesign.html` (`#processing`).

## Goals

1. The upload bar carries on after upload and shows an **estimated** progress, the way
   uploads show one today. One place answers "is it finished yet?".
2. It **survives sessions**: close the tab, reload, switch phone to laptop, and the same
   bar comes back with the current numbers. The server owns the state; no browser has to
   stay open.
3. Updates travel by **Web PubSub on the Free tier** without spending the daily quota.
   Vision, the longest stage, reports in **10% steps**.
4. Polling stays as the fallback, so a dropped or over-budget message only delays the
   bar and never breaks it.

## What exists today (verified in `origin/main`)

- Wave states, in order: `collecting` → `processing_pending`/`processing` (vision) →
  `ready` (clustering) → `indexing` → `complete` (`backend/upload_waves.py`).
- `GET /api/upload-sessions/<session>/processing` (`backend/routes/upload_sessions.py`)
  answers only `{done:false,state}` until the wave completes, then
  `{done:true, processedSeconds, failed, people}`. **There is no mid-stage progress.**
- One push, `publish_processing(library, wave_id, 'done')`, fired from
  `execute_wave_handoffs` (`backend/wave_runtime.py`) when a wave completes. The client
  (`frontend/src/services/indexReadyPush.ts`) treats pushes as invalidation: it re-reads
  the summary. Polling is 2 min, or 10 min with the socket up.
- The watcher is keyed by **upload session id** and runs for at most 3 h
  (`PROCESSING_WATCH_MS`), so it does not survive a new browser or a long stage.
- Vision already commits **per page of members** and writes a durable checkpoint after
  each page (`checkpoint('sources', cursor)` in `run_vision_release`,
  `backend/upload_wave_execution.py`). A wave knows its `memberCount` up front.
  That is the natural, free place to count progress.
- The mockup deliberately shows no percentage ("Keepsake can't say how many photos are
  done"). This design reverses that for vision and keeps the other two stages as
  moving segments until we can estimate them (see Open questions).

## Data model

Progress is a small record on the wave row's existing neighbours, so it adds no new
table and no extra writes in the hot path.

```
progress = {
  stage:        'vision' | 'cluster' | 'index',
  done:         int,       # members finished in this stage
  total:        int,       # wave.memberCount (frozen at stage start)
  stageStartedAt, updatedAt: epoch seconds,
  rate:         float,     # members/s, smoothed
  etaSeconds:   int | null # null until enough samples
}
```

- **Vision** writes `done` and `updatedAt` in the same call as the page checkpoint it
  already makes (one more field, zero more requests). `done` is the count of members
  whose receipts committed, not the cursor position, so retries never overcount.
- **Cluster and index** write `stage` and timestamps at start and end. They report
  `done` only if a cheap counter exists in their release code; otherwise they are
  indeterminate (moving segment) and contribute time, not fraction.
- Stored on the wave (`jobs` table, `e!<wave>` row family) so it is as durable as the wave.

## Estimation

- **Per-stage rate**: exponentially smoothed members/second over the last few page
  commits of this wave. Before 3 samples there is no ETA, only "Getting started".
- **Seed**: when a wave starts, use the library's last completed waves' rates (a small
  `rates!` row, median of the last five). Vision photos/hour has varied a lot (about 7k
  → 4k/hr in the 2026-11 benchmarks, see memory notes), so the seed is only used for
  the first ETA and is replaced by live samples.
- **Overall bar**: weight each stage by its expected seconds for this member count
  (members ÷ rate, from the same history; default weights until history exists) and sum
  the finished fractions. Vision dominates, so the bar mostly moves during vision.
- **Never move backwards.** The client clamps the displayed fraction to the max it has
  shown for that wave. If reality is slower than the estimate, the copy changes to
  "Taking longer than expected" instead of the bar retreating.
- **Coarse time copy**: "about 2 min", "about 10 min", "about an hour", never seconds,
  and "Estimated" in the detail sheet. Rounding is what makes a rough number honest.

## Push design and rationing

Free tier: 20 concurrent connections, 20,000 messages/day (see
`memory/project_index_push_free_tier_only.md`; we stay on Free). Assumption to verify:
a group message counts once **per receiving connection**, so cost is events × watchers.

**Events per wave (upper bound)**

| Stage | Messages | Rule |
|---|---|---|
| Vision | 10 | one each time `floor(done × 10 / total)` increases, plus a 0% start |
| Cluster | 2 | start, end |
| Index | 2 | start, end |
| Done | 1 | existing `processing` done message |
| **Total** | **≈ 15** | independent of photo count |

A 16k-photo wave and a 10-photo wave both cost about 15 events. For a wave with
`total < 50` skip step messages and send only stage changes and done.

**Rules that keep it inside the quota**

1. **Step, not tick**: a message is sent only when a step boundary is crossed, never
   per page. Crossing several steps in one commit sends the latest one only.
2. **Min interval**: at most one progress message per library per 30 s.
3. **Daily budget per sending process**, next to the existing
   `PROCESSING_DAILY_BUDGET`. Proposed `PROGRESS_DAILY_BUDGET` (default 3000 events).
   Over budget, progress stops and clients fall back to polling. `done` is never
   budgeted away.
4. **Worst case**: 20 watchers × 15 events = 300 messages per wave, so about 60 waves a
   day before the 20,000 cap, ahead of index-ready traffic. Realistic use is 1-3
   waves with 1-3 watchers.
5. **Payload carries data**: `{type:'processing-progress', libraryId, waveId, stage,
   step, etaBucket}`, under 200 bytes (one message unit). Unlike today's pure
   invalidation, the client can draw from it without a read. The GET below stays
   authoritative and the push is advisory.

**Fallback**: socket connected → one safety GET every 2 min. Socket down or over
budget → GET every 20 s while the bar is visible and the tab is in the foreground,
nothing in the background. This reuses `onConnection` from `startIndexReadyPush`.

## Surviving sessions

- New endpoint `GET /api/processing` (library-scoped, `Cache-Control: no-store`) returns
  every active wave of the signed-in library: `{waves:[{waveId, stage, done, total,
  etaSeconds, startedAt, steps:[{name,state,result}]}]}`, plus waves finished in the
  last 24 h not yet dismissed. It replaces the session-id lookup, so it works from any
  browser and after the 3 h watcher window.
- The client calls it on load and on foreground return, and after every push. The bar
  renders from it, so reload and second device show the same state.
- **Dismissal** is server-side (`POST /api/processing/<wave>/dismiss`, per user) so a
  finished bar stays dismissed on every device. Until dismissed it stays, as in the
  "Finished" mockup.
- Library members: show it to anyone who can upload to that library, read-only for others.

## Frontend

- `ProcessingProgressStore` fed by the push (`onProcessing` gets the payload) and the
  GET; exposes `{stage, fraction, etaLabel, steps, done}`.
- The existing `upbar` switches to the `seg3` three-segment bar after the last upload;
  the current segment shows the vision fraction when known, the sliding indeterminate
  animation otherwise. Copy comes from the mockup ("Reading your photos", "Finding
  people", "Getting search ready"). Detail sheet adds "Estimated, about N min left".
- Finished steps keep their result ("9 people found") from the existing summary.

## Notifications (the user-facing count)

The bar carries progress; the **Notifications list gets exactly one entry per wave**: a
"done" entry, as the mockup says. 10% steps never create notification entries or
toasts. A stalled wave (no progress for the existing stall threshold) adds one "taking
longer than expected" entry, replaced by the done entry later, so the count stays one
per wave.

## Rollout

1. Backend: write `progress` with the vision page checkpoint; add `GET /api/processing`
   and dismiss. No behaviour change for clients that do not call it.
2. Publish step events with `PROCESSING_PROGRESS_PUSH` on by default (`false` turns it off), with the
   budget and min-interval guards and a diagnostics event per send/skip.
3. Frontend store and bar; keep the polling fallback until push is verified live.
4. Add cluster/index fractions only if their counters are cheap (decision after step 3
   data).

Deploy only when no upload is running (rollouts abort browser sessions; see
`memory/feedback_redeploy_during_active_upload.md`).

## Open questions

- **Quota accounting**: confirm that group fan-out counts per connection. If not,
  the budget is looser than stated.
- **Cluster/index counters**: do their release loops have a cheap `done` count? If
  not, they stay indeterminate and the estimate for them is time-based only.
- **Mockup update**: the mockup shows no percentage. The designer should add the
  estimated state: a fraction on the current segment, "about N min left", and
  "taking longer than expected". The mockup is currently an uncommitted change in the
  main checkout, so this doc does not edit it.
- **Live push**: as of 2026-11-18 the browser socket only connects after index activity,
  and a post-deploy test upload showed no delivered messages (the cause is not yet
  known). Fix or explain that first, since this design depends on the socket.
- **Wave merging**: several uploads can share one wave; the bar then shows the wave's
  total, not the user's own photos. Decide whether that is acceptable copy.
- **Skip publish when nobody is watching**: a connection-count check costs a REST call;
  only worth it if the budget proves tight.
