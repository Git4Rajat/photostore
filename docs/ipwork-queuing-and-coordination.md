# Photo processing: queuing, browser/ipworker split, leases, and re-queueing (as of 2026-08-28)

Scope: a focused deep-dive on the *dispatch and coordination* layer of photo
processing — how a photo's six processing steps
(`thumbnail`/`exif`/`ocr`/`face`/`ai_vision`/`map_detection`) get discovered
and claimed by a worker, how the browser and ipworker paths avoid redoing
each other's work, what "a lease breaks" actually means operationally, and
every path by which a photo gets re-queued. This is a companion to
[docs/ipworker-architecture.md](ipworker-architecture.md) (hardware,
concurrency, per-step cost, OCR experiments) — that doc covers *how fast
one photo processes*; this one covers *how a photo gets picked up at all,
and what happens when that goes wrong*. Everything below is read directly
from `backend/app.py`, `backend/storage_utils.py`,
`frontend/src/components/AppServicesProvider.tsx`, and
`deploy/resources.bicep`, not recalled from memory.

Written as prep material for a review of this subsystem — the last section
lists the rough edges this doc surfaced, as a starting point for that
review, not a finished set of recommendations.

## 1. The mental model: one producer-side signal, two independent consumers, one lock

There is no single "processing queue." There are **two separate
work-discovery mechanisms**, one per consumer, plus a shared **per-photo
lease** that keeps them from doing the same work twice:

| | ipworker | browser |
|---|---|---|
| Work-discovery | Real Azure Storage Queue (`photostore-ipwork`), push model | HTTP poll (`GET /upload/processing/pending`), pull model |
| Unit of work | One message = one (photo, step-list) | One poll response = a batch of photos with per-step status |
| Self-healing for missed work | Sweep thread re-enqueues onto the same queue | N/A — poll always re-scans live status, nothing to "miss" |
| Coordination primitive | `processing_lease_*` fields on the metadata row (shared with browser) |

Both consumers, when they decide to actually do work on a photo, first
**claim the same lease** (`claim_processing_lease`,
`backend/storage_utils.py:3013`). Whoever holds it does the work; the loser
backs off. This lease is the only thing preventing duplicate inference —
the queue and the poll are just two different ways of finding candidate
photos to attempt.

## 2. How ipworker discovers work: the real queue

Queue name `photostore-ipwork` (`IPWORKER_QUEUE_NAME`,
`backend/app.py:389`), created at backend startup. One message = one
`{jobId, correlationId, user_id, filename, steps}` payload
(`_queue_ipwork_processing`, `backend/app.py:9615`).

**Two ways a message gets onto this queue:**

1. **Direct enqueue at upload time** — `_queue_upload_processing`
   (`backend/app.py:9653`) calls `_queue_ipwork_processing` for every
   fresh upload, unconditionally requesting all of `IPWORK_STEPS`. No-op
   if `PROCESSING_MODE == 'browser'`.
2. **The sweep** (`_ipwork_sweep_loop`, `backend/app.py:10251`) — see §6.

`_queue_ipwork_processing` is also called ad hoc by other code paths (e.g.
an admin backfill scoped to `{"steps": ["ocr"]}`, or a face-embedding
version bump) — the queue is a general "ipworker, please look at this
photo" channel, not exclusively an upload-time hook.

Nothing about this queue is FIFO-guaranteed or exactly-once in the way the
name "queue" might suggest — Azure Storage Queues are at-least-once
delivery with a visibility-timeout redelivery model (§7), and this system
deliberately relies on that redelivery for one of its retry paths.

## 3. How the browser discovers work: polling, not a queue

There is no browser-side queue at all. `GET /upload/processing/pending`
(`backend/app.py:10276`) does a live scan of the calling user's metadata
rows (`_query_metadata_rows_for_user`, projected to
`BROWSER_PROCESSING_PENDING_SELECT`) and, per row, computes whether
anything is actually pending right now via `_browser_processing_pending_item`
(`backend/app.py:10046`):

- A step whose status isn't a terminal status (`BROWSER_PROCESSING_TERMINAL_STATUSES
  = {done, no_data, deleted, skipped, unsupported, failed, timeout}`,
  `backend/app.py:9915`) counts as pending.
- A step stuck `running` **is treated as pending again** if its lease has
  expired (`_browser_processing_lease_expired`, `backend/app.py:9932`) —
  this is the browser's entire mechanism for "recovering" a photo whose
  previous owner (another tab, or ipworker) died mid-processing. There's no
  separate re-queue step; the next poll just sees it as pending again
  because the lease field says so.
- `ai_vision` gets a narrow retry carve-out for RAW files whose vision step
  failed for an infrastructure reason (`_raw_ai_vision_no_data_should_retry`,
  `RAW_AI_VISION_RETRY_REASONS`, `backend/app.py:9963`) even though its
  status is nominally terminal.
- `face` gets reopened if the stored embedding predates
  `FACE_CLUSTER_EMBEDDING_VERSION` (`_browser_processing_face_version_stale`,
  `backend/app.py:10020`), regardless of its terminal status.

Results are sorted oldest-`last_processing_update`-first and capped to
`limit` (`PENDING_PROCESSING_BATCH_SIZE = 40`,
`AppServicesProvider.tsx:413`). The frontend calls this once per drain
cycle to refill a batch for however many concurrent "lanes" browser
processing is running (`AppServicesProvider.tsx:1688-1710`), not once per
photo.

**Consequence:** the browser's queue is entirely derived, on every call,
from the same status fields the lease and the sweep also read. There's
nothing to desynchronize *within* the browser's own view — but this also
means the browser only ever sees its own library's photos, scoped to
whichever tab is open and polling. A photo in a library nobody has open in
a browser tab is invisible to this path entirely, which is exactly the gap
the sweep exists to cover for ipworker (§6) and which the browser has no
equivalent self-heal for at all — see §9.

## 4. The browser/ipworker split: `PROCESSING_MODE` and step ownership

`PROCESSING_MODE` (`browser` / `backend` / `both`) is a single deploy-time
setting (`deploy/resources.bicep:218`, `APP_CONFIG_PROCESSING_MODE` served
to the frontend at `:564`) that governs three independent things:

1. **Whether the backend enqueues ipwork messages at all**
   (`_queue_ipwork_processing` no-ops in `browser` mode,
   `backend/app.py:9628`).
2. **Whether the frontend's client-side AI pipeline runs at all**
   (`AppServicesProvider.tsx`, gated off `getRuntimeConfig().processingMode`).
3. **Step ownership inside `both`/`backend` mode** — `thumbnail` and `exif`
   are hard-carved-out as ipworker-exclusive once `PROCESSING_MODE ===
   'backend'` (`BACKEND_MODE_OWNED_STEPS`, `restrictStepsForProcessingMode`,
   `AppServicesProvider.tsx:473-480`): the browser drops those two steps
   from its own request set entirely rather than attempting them, "so this
   queue-picker doesn't download a photo … just to discover there's nothing
   left the browser can actually do with it." In `both` mode this
   restriction does **not** apply — thumbnail/exif are up for grabs by
   either side, same as every other step, and the lease decides the winner.

So `both` mode is a genuine race on every step for every photo: browser and
ipworker both may attempt `claim_processing_lease` for the same photo, and
whichever call lands first wins (§5). `backend` mode narrows that race to
four of the six steps (the two AI-gated exclusions don't change; only
thumbnail/exif get pre-filtered browser-side before a claim is even
attempted).

## 5. The lease: the actual coordination primitive

Three fields live directly on the photo's metadata row (no separate lease
table): `processing_lease_owner`, `processing_lease_expires_at`, plus one
`{step}_status` per step. Three operations, all in
`backend/storage_utils.py`:

- **`claim_processing_lease`** (`:3013`) — fails loudly (raises) if
  `processing_lease_owner` is set to someone else *and* not expired.
  Otherwise takes over: sets owner, sets a fresh expiry
  (`now + lease_seconds`), and flips every requested step's status to
  `running` (unless already terminal). This status flip is itself the
  mechanism that makes a claimed-but-abandoned photo show up as "pending"
  again once the lease lapses (§3) — there's no separate cleanup job for
  it.
- **`heartbeat_processing_lease`** (`:3065`) — re-claims (extends expiry)
  without touching step statuses (`mark_running=False`), so a long-running
  step doesn't need its status flipped again mid-flight.
- **`release_processing_lease`** (`:3080`) — clears the two lease fields,
  but *only if the caller is still the recognized owner* (a no-op
  otherwise, so a late release from a lease you already lost can't
  clobber whoever took over).

**Lease durations differ by owner, deliberately:** browser claims 120s
(`claim_processing_lease(..., lease_seconds=120, ...)`,
`backend/app.py:10339`, matching `CLIENT_PROCESSING_LEASE_SECONDS`);
ipworker claims `IPWORKER_LEASE_SECONDS = 300` (`backend/app.py:404`) —
5x longer, because one ipwork pass runs all requested steps **sequentially
in a single message** (download once, then
thumbnail→exif→ocr→face→ai_vision→map_detection,
`_run_ipwork_steps`), whereas the browser's 120s covers one step's worth of
work at a time and re-claims per photo per drain iteration.

The browser additionally sends an explicit heartbeat mid-flight
(`POST /upload/processing/heartbeat`, `AppServicesProvider.tsx:1785`) right
after claiming, precisely because 120s can be tight for a slow on-device
model pass; ipworker never heartbats — its whole budget is front-loaded
into the 300s claim and it's expected to finish within that window in one
shot.

## 6. Lease breakage: every way it actually happens

"The lease breaks" isn't one event — it's several distinct scenarios, each
with different consequences and different recovery paths:

**a) Owner finishes normally.** `apply_client_processing_results_for_file`
(`backend/storage_utils.py:2704`) clears `processing_lease_owner`/
`processing_lease_expires_at` unconditionally as part of writing results
(`:2750-2752`). Not a "breakage" — the clean-exit path. Both the browser
(`/upload/client-processing`) and ipworker (`apply_client_processing_results_for_file`
called in-process, `backend/app.py:12876`) go through this same function.

**b) Owner dies before finishing, lease simply expires.** A browser tab
closes mid-processing, or an ipworker replica crashes/is killed
(`ThreadPoolExecutor` future never completes). Nobody calls `release`; the
lease just sits until `processing_lease_expires_at` passes. Until then, the
photo looks actively "in progress" to everyone — the browser's poll won't
re-offer it (`_browser_processing_lease_expired` returns `False`), and
another `claim_processing_lease` call raises. After expiry, it's fair game
again to whoever asks next: `claim_processing_lease` explicitly checks
`lease_expired` and allows a takeover even with a stale `owner` string
still present (`backend/storage_utils.py:3033`).

**c) Owner explicitly loses a claim race.** `claim_processing_lease` raises
`RuntimeError('Processing lease is already held by another client.')` when
someone else holds a live lease. On the ipworker side this becomes a
`'lease_busy'` outcome (`_handle_ipwork_queue_payload`,
`backend/app.py:12838-12842`) — not an error, a normal "someone else is
already on it" signal. This is the `both`-mode race path from §4: whichever
of browser/ipworker calls `claim_processing_lease` first this cycle wins
outright; there's no queueing or fairness, first request wins.

**d) A step finishes despite losing the lease.** `_step_locked_done`
(`backend/storage_utils.py:2085`) is a second, independent guard applied at
*write* time, not claim time: even if a straggler somehow finishes
computing a step after another owner already wrote a `done` result for it
(e.g. a very slow ipwork pass whose lease expired and got reclaimed
mid-flight), the straggler's write for that already-done step is dropped.
This is explicitly called out as "a second line of defense in case a lease
expired mid-flight and got reclaimed" (`backend/app.py:12816-12819`) — the
lease alone doesn't fully prevent a late write, this does.

None of these are bugs — they're the intended behavior of a
best-effort, no-fairness, expiry-based lock. The rough edges are in the
*timing* of how long a photo sits in a broken-lease state before something
notices (§9).

## 7. Re-queueing: what happens to a dequeued-but-unfinished message

This is the part that's specific to ipworker, since only it has a real
queue. Two independent mechanisms re-queue a photo; they solve different
problems.

### 7a. Azure Queue redelivery (same message, same queue)

`receive_messages()` is called with an explicit
`visibility_timeout=IPWORKER_VISIBILITY_TIMEOUT_SECONDS` (300s,
`run_ipworker`, `backend/app.py:13048`). A received-but-not-yet-deleted
message becomes invisible to other consumers for that window, then
automatically reappears (Azure's standard at-least-once semantics) with
`dequeue_count` incremented.

ipworker uses this redelivery **on purpose** as a retry mechanism for one
specific case: it lost the lease race to the browser (`'lease_busy'`), but
the browser might abandon the photo before finishing. Rather than deleting
the message and losing the work forever, the main loop leaves it undeleted
whenever `outcome == 'lease_busy'` **and** `dequeue_count <
IPWORK_LEASE_RETRY_LIMIT` (3, `backend/app.py:13079`) — Azure redelivers it
~300s later, ipworker tries the lease claim again, and if the browser tab
really did close, the lease has long since expired (120s browser lease vs.
300s retry wait) and ipworker wins on this attempt. Once
`dequeue_count >= IPWORK_LEASE_RETRY_LIMIT`, the message is deleted
unconditionally — ipworker gives up, whether or not the work ever
completed (the sweep, §7b, is the only backstop after that).

**Why the visibility timeout has to match the lease TTL, not just be "long
enough":** if `visibility_timeout` were shorter than one photo's real
processing time (Azure's own default is 30s, far too short for a
multi-step inference pass), Azure would redeliver the *same in-progress*
message to a second replica while the first is still working it. Because
the redelivered copy carries the identical `jobId` (hence identical lease-
owner string, `lease_owner = f'ipworker-{job_id}'`,
`backend/app.py:12833`), `claim_processing_lease`'s ownership check
**would not catch this** — the second replica would see "I already own
this lease" and proceed, running the full model pipeline concurrently with
the first (`backend/app.py:417-429`, comment on
`IPWORKER_VISIBILITY_TIMEOUT_SECONDS`). Setting the visibility timeout
equal to `IPWORKER_LEASE_SECONDS` closes this specific gap by construction
— it's not a generic safety margin, it's matched to a concrete duplicate-
processing scenario.

### 7b. The sweep (independent re-enqueue, not a redelivery)

`_sweep_stale_processing_into_ipwork` (`backend/app.py:10207`) is the other
re-queueing path, and it solves a different problem: a photo that **never
had a message on the queue at all** (uploaded while ipworker was stopped,
or during a `browser`-only `PROCESSING_MODE` window), or whose message is
presumed permanently lost. Unlike §7a, this doesn't rely on Azure
redelivery — it scans metadata directly, across **every library**, not
just whichever library a currently-open browser tab has active
(`backend/app.py:10213-10217`), and calls `_queue_ipwork_processing` fresh
for anything it finds eligible.

`_ipwork_sweep_eligible_steps` (`backend/app.py:10154`) decides eligibility
per step, mirroring `_browser_processing_pending_item`'s notion of "not
done" but with one extra guard specific to *actively* re-enqueuing (as
opposed to the browser's read-only poll): a step sitting at `queued`
status is left alone unless it's been stuck there for at least
`IPWORK_SWEEP_STALE_QUEUED_SECONDS` (1800s / 30min,
`backend/app.py:10100`) — "otherwise every sweep interval would pile a
fresh duplicate message onto a perfectly healthy backlog." A `running` step
is only re-offered once its lease has actually expired (same
`_browser_processing_lease_expired` check as everywhere else); a stale
face-embedding-version photo is re-offered regardless of its terminal
status, same carve-out as §3.

Runs on its own daemon thread inside every ipworker replica
(`_ipwork_sweep_loop`, `backend/app.py:10251`), on a fixed
`IPWORK_SWEEP_INTERVAL_SECONDS` (1200s / 20min) cadence, independent of the
queue-polling loop so a slow/large sweep never delays picking up fresh
messages. Because ipworker scales to zero when idle, a cron scale rule
(`ipwork-sweep-wake`, `deploy/resources.bicep:790`) wakes one replica for a
5-minute window every 20 minutes purely so the sweep gets a chance to run
even with an empty queue — kept in sync with `IPWORK_SWEEP_INTERVAL_SECONDS`
by convention (two separately-edited numbers, not derived from one
another).

**Fixed 2026-08-28 (`f58ad31`): the sweep used to run independently on
every replica.** With `maxReplicas: 4`, all four replicas' sweep threads
would fire on the same ~20-minute cadence and each independently re-enqueue
the *same* stale backlog — confirmed live during a `photostore-test`
backfill to blow queue depth up to ~5x the real photo count. The fix
(`_try_claim_ipwork_sweep_lock`, `backend/app.py:10104`) is a
create-then-steal-if-expired lock row (mirrors the existing delegation-key
claim pattern) keyed on a fixed row (`ipwork_sweep_lock`) in the metadata
table: each replica's loop iteration first tries to claim this lock for
`IPWORK_SWEEP_INTERVAL_SECONDS`, and only the winner actually runs
`_sweep_stale_processing_into_ipwork()` that cycle — the other replicas'
iterations become no-ops. This means the sweep is now, for practical
purposes, effectively single-instance despite `maxReplicas: 4`. Note the
cron rule's `desiredReplicas: 1` alone did **not** prevent this bug — the
queue-length rule can independently push replica count above 1 during the
same 5-minute wake window if there's also a real backlog, which is exactly
the condition (a backfill) under which this was caught.

### 7c. Why redelivery/re-enqueue duplication is safe, independent of the lease

Both §7a and §7b can, in principle, produce more than one live message for
the same photo (e.g. a lease-busy retry from §7a arriving in the same
window as a sweep re-enqueue from §7b). This is tolerated by design at two
layers, not just the lease:

- `_handle_ipwork_queue_payload` recomputes `runnable_steps` fresh from
  the lease response's own `statuses`, dropping any step someone else
  already finished while the message sat in the queue
  (`backend/app.py:12843-12850`) — "this is free and avoids redoing
  completed inference." If every requested step turns out already done, it
  releases the lease immediately and returns `'noop'` without running
  anything.
  - One explicit exception: a `done` `face_status` doesn't necessarily mean
    current — if the stored embedding is on a stale version, `face` is
    added back into `runnable_steps` even though the lease reported it
    `done` (`backend/app.py:12861-12864`), otherwise every sweep-driven
    stale-embedding re-offer would immediately no-op itself away, "exactly
    what happened the first time the sweep ran against a real
    stale-embedding-version backlog."
- `_step_locked_done` (§6d) catches the remaining case where two attempts
  both got far enough to compute a result before either wrote it.

## 8. Autoscaling's dependency on §7a

The KEDA `azure-queue` scale rule on `photostore-ipwork`
(`deploy/resources.bicep:750-774`) uses `queueLengthStrategy: 'visibleonly'`
instead of the default (`'all'`) specifically because of §7a: a message
deliberately left undeleted for a lease-busy retry is real queue depth
under the default strategy, and would scale ipworker up to handle photos
the browser had already finished. `visibleonly` counts only genuinely
unclaimed (visible) messages — it falls back to `'all'` above 32 messages,
so a genuinely large backlog still scales correctly; this only changes
behavior in the small-numbers case that lease-busy retries actually
produce.

## 9. Rough edges surfaced while writing this doc

Not fixes — flagged for the review this doc is prep for.

- **Two unrelated retry mechanisms answer the same question differently.**
  A browser-abandoned photo recovers via §7a (queue redelivery, ipworker
  side) *or* via the next poll noticing an expired lease (browser side,
  §3) — whichever consumer happens to ask again first. There's no single
  "this photo's lease broke, here's what happens next" path to reason
  about; it's two independent polling/redelivery loops that happen to
  converge on the same lease fields.
- **Worst-case abandon recovery window stacks multiplicatively.** A photo
  whose owner truly disappears can wait: up to 120s (browser lease TTL) +
  up to `IPWORK_LEASE_RETRY_LIMIT × IPWORKER_VISIBILITY_TIMEOUT_SECONDS`
  (3 × 300s = 900s) if ipworker was the one that lost the race and is
  retrying via §7a — nearly 17 minutes — before the retry budget is
  exhausted and the *only* remaining path is the sweep, which itself only
  runs every 20 minutes and additionally requires the step to have been
  `queued`-stale for 30 minutes before it'll touch it. A photo that falls
  through every fast path can plausibly sit for 30-50 minutes before
  self-healing.
- **The browser side has no sweep-equivalent at all.** If nobody ever
  opens a browser tab against a given library again, the sweep (ipworker)
  is the *only* self-healing path left for that library — the browser's
  own "expired lease → pending again" logic (§3) only fires when a tab is
  open and polling. In `browser`-only `PROCESSING_MODE` (no ipworker
  deployed at all), an abandoned lease has no recovery path except that
  same library's browser eventually reopening.
- **The two TTLs that must stay matched (`IPWORKER_LEASE_SECONDS` and
  `IPWORKER_VISIBILITY_TIMEOUT_SECONDS`) are two independently-edited
  numbers, not one derived from the other** — same shape as the
  `IPWORK_SWEEP_INTERVAL_SECONDS`/cron-schedule pairing in §7b. Both pairs
  are currently correct by convention and by comment, not by any
  structural guarantee; the concurrency=3 vCPU drift and the sweep's
  N-replicas bug (§7b) are both prior incidents of exactly this class of
  "two numbers that have to agree silently drifted apart."
- **Retry budget (`IPWORK_LEASE_RETRY_LIMIT=3`) is a flat count, not
  adaptive to why the lease is busy.** The same 3-retries-at-300s-each
  policy applies whether the browser tab is about to finish in 2 more
  seconds or has already been closed for an hour — no way to distinguish
  "genuinely still working" from "abandoned" other than waiting out the
  full budget either way.
- **Batched persistence compounds re-queueing risk, not just latency.**
  Per [ipworker-architecture.md §4](ipworker-architecture.md), nothing
  from a photo's step sequence is written until *all* requested steps
  finish — so if ipworker crashes partway through (e.g. after `ocr`, before
  `face`), the already-computed `thumbnail`/`exif`/`ocr` results are lost
  entirely, not just delayed, and the eventual retry/sweep re-enqueue
  redoes the full sequence including the already-finished expensive
  steps (OCR being the most expensive one, per that doc's §6).
- **The sweep singleton lock (§7b) was fixed same-day as this doc.** Worth
  confirming under a live backfill that the fix actually holds at
  `maxReplicas: 4` with the queue-length rule pushing replica count up
  concurrently with the cron wake window — the exact condition that
  produced the original 5x blowup.

## 10. Reference: every timing/config knob in this system

| Knob | Value | Meaning | Location |
|---|---|---|---|
| `PROCESSING_MODE` | `browser` / `backend` / `both` | Gates ipwork enqueue + frontend AI pipeline + backend/both step-ownership split | `backend/app.py`, `deploy/resources.bicep:218` |
| `CLIENT_PROCESSING_LEASE_SECONDS` (browser lease) | 120s | How long a browser claim holds the lease | `backend/storage_utils.py:2931`; passed as a literal `120` at the call site, `backend/app.py:10339` |
| `IPWORKER_LEASE_SECONDS` | 300s | How long an ipworker claim holds the lease | `backend/app.py:404` |
| `IPWORKER_VISIBILITY_TIMEOUT_SECONDS` | 300s | Azure queue redelivery window; must match the lease TTL above | `backend/app.py:432` |
| `IPWORK_LEASE_RETRY_LIMIT` | 3 | Max redeliveries before ipworker gives up on a lease-busy message | `backend/app.py:416` |
| `IPWORKER_CONCURRENCY` | 2 | In-replica thread-pool size; also caps in-flight message count | `backend/app.py:442` (see ipworker-architecture.md §4) |
| `IPWORKER_POLL_SECONDS` | 2s | Main loop's fetch/wait cadence | `backend/app.py:13013` |
| `IPWORK_SWEEP_INTERVAL_SECONDS` | 1200s (20min) | Sweep cadence per replica (only the lock-winner's scan actually runs) | `backend/app.py:10099` |
| `IPWORK_SWEEP_STALE_QUEUED_SECONDS` | 1800s (30min) | How long a `queued` step must sit before the sweep will re-offer it | `backend/app.py:10100` |
| `ipwork_sweep_lock` TTL | = `IPWORK_SWEEP_INTERVAL_SECONDS` | Cluster-wide singleton lock for the sweep | `backend/app.py:10264` |
| `ipwork-sweep-wake` cron | `0,20,40 → 5,25,45 * * * *` UTC | Wakes 1 replica so the sweep can run even at queue-depth 0 | `deploy/resources.bicep:790` |
| KEDA `ipwork-queue` rule | `queueLength: 1`, `visibleonly` | Replica autoscale trigger; ignores undeleted lease-busy retries | `deploy/resources.bicep:750` |
| `PENDING_PROCESSING_BATCH_SIZE` | 40 | Browser poll batch size per `/upload/processing/pending` call | `AppServicesProvider.tsx:413` |
| ipworker replicas | 0 min / 4 max | | `deploy/resources.bicep:740,748` |
