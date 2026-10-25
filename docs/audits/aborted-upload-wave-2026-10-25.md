# Aborted upload wave validation — 2026-10-25

## Expected behavior

Stop cancels unfinished transfers and queued transport batches. Photos whose
bytes have already committed must finish finalization and remain accepted wave
members. Remaining reservations resolve as cancelled; closing intake allows the
accepted subset to finish processing, clustering, and publication.

## Local findings

- Ordinary Stop already closes intake for both 50-file selections and 901-file
  selections spanning the 900-file transport boundary. Tests exercise both live
  and shadow frontend wiring, with one accepted photo and the rest cancelled.
- A delayed-finalization regression failed before the fix: Stop included the
  committed photo in `/upload/cancel` while `/upload/finalize-batch` was still
  pending. Cleanup could therefore remove resources finalization still needed.
  This establishes the race locally; it does not establish the cause of a
  particular deployed stuck wave.
- Cleanup now waits in the background for this run's pending finalizations and
  overlays their terminal outcomes before choosing unfinished artifacts. Stop
  remains responsive, and confirmed photos are excluded from cancellation even
  after the persisted session has been cleared.
- A real Flask route test confirms accepted-plus-cancelled intake automatically
  closes and dispatches vision with only the accepted member.
- A runtime regression confirms a stopped 25-item selection with 20 accepted
  photos and 5 cancellations reaches `complete` through vision, cluster, and
  index consumers. Storage, inference receipts, and catalog publication use test
  fakes; this is not a deployed browser/storage end-to-end test.

## Validation

- Frontend upload provider, wave synchronization, wave operations, and wave
  client suites: **79 passed**.
- Backend live routes, wave liveness, recovery, and execution suites:
  **59 passed**.
- `npm run typecheck`: passed.
- `git diff --check`: passed.

## Remaining improvement opportunity

Wave resolution and close calls still use best-effort, single-attempt requests.
A failed cancellation resolution can leave intake open until another attempt or
scheduled recovery. Recovery can abandon inactive intake after 3,600 seconds
without wave progress. A durable, bounded retry for Stop settlement would reduce
that delay; this change addresses the confirmed cleanup/finalization race.

No deployment or production data changes were performed.
