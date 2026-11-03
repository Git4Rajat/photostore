# Wave continuations and recovery with zero replica floors

All PhotoStore apps retain minimum replicas of zero. Queue deliveries and HTTP
requests are the activation mechanisms. No cron scaler, scheduled recovery job,
periodic failure revival, or timer-based barrier polling is required by this fix.

## Keep active work visible to autoscaling

The main Vision, clustering and indexing queues count both visible and invisible
messages. Their in-flight deliveries therefore keep a replica active while it
owns real queued work, without changing its configured replica floor.

Clustering and indexing also have visible-only deferred queues. A due durable
wave delivery is copied to its main queue before the deferred delivery is
acknowledged. Ordinary durable index maintenance follows the same rule. Work is
never executed from those visible-only deliveries. A failed send retains the
source; exhausted transfer failures preserve a replayable dead letter. An ACK
failure may cause a duplicate, which the durable stage/dispatch fences tolerate.

## Progress, dependencies and failures are different

- Productive slices checkpoint before sending an immediate main-queue
  continuation. Confirmed checkpoint or wave-scan advancement resets consecutive
  failures for Vision, clustering and indexing.
- A predecessor publication barrier is handled by outbox eligibility and the
  predecessor's durable completion handoff. An ineligible successor does not
  schedule itself to poll the dependency.
- A live dispatcher or stage owner retains the existing receipt without taking
  another failed attempt or generating a replacement wake. A racing stage claim
  refunds the dispatcher attempt. Stage cleanup releases only its exact owner.
- A pass that makes no durable progress retains the existing queued delivery and
  consumes the bounded consecutive-failure budget. It does not manufacture an
  unlimited series of refunded `barrier_wait` messages. Exhausted dispatches stay
  failed until an explicit operator retry; unrelated lifecycle events do not
  revive them after an hourly cooldown.
- Existing bounded retries for actual source/coordination failures remain queue
  work. They are not periodic background schedules.

Stage transitions and queue sends are not a distributed transaction. If a send
fails after a new stage commits, the original retained delivery repairs the
current stage's handoff on redelivery. Completed-wave handoffs retain their
existing durable per-handoff completion records.

## Resume indexing without rebuilding completed outputs

The index execution checkpoint records the preparation phase/member cursor,
published-scope progress, and the final per-source publication-marker cursor.
Preparation and final bookkeeping yield only between committed pages. A crash
inside a page replays that page; it cannot advance past an uncommitted marker.

Previously satisfied scope receipts avoid rebuilding outputs. The real catalog
adapter still verifies their immutable publication proofs and blobs before
completion; a missing proof or changed generation fails closed. An interrupted
legacy wave with all eight receipts skips metadata preparation/builds and resumes
completion markers. The last completion transaction and existing successor
handoffs remain authoritative.

A normal stop or exception releases the exact clustering/index execution lease
just as Vision already did. A hard process kill still relies on finite lease
expiry and redelivery of its existing message, with journal/receipt replay.

## Evidence correction

The November 3 evening wakeup report initially classified clustering's repeated
`barrier_wait` starts as doing no new assignment work. Its saved detailed records
also show processed-face checkpoints increasing from 128 through 384, 640, and
ultimately 3,060. Those were productive bounded slices, incorrectly classified
using the wave row's unchanged `lastProgressAt`; the approximately 40–50 ms
`lease_wait` phases do not demonstrate long lease contention. The fix uses the
execution checkpoint as well as wave state to recognize progress, and sends
immediate main-queue continuations to avoid another cold start per slice.
The 32 total recorded scale-from-zero events cannot all be classified as waste.

## Verification and rollout

Regression coverage includes live-owner claim races, more than five productive
clustering slices, bounded no-progress behavior, failed stage handoff sends,
predecessor dependency waits, deferred-to-main transfer ordering/failures,
checkpointed index preparation and publication, interruption halfway through a
marker page, and missing immutable output proofs. Deployment-template checks
reject cron scalers, scheduled jobs, and positive replica floors.

These are source changes. Existing failed releases require an explicit retry
after the new worker images are deployed and verified; this change does not
silently reset their budgets or assert that they have completed.
