# Upload wave step 3: execution -- audit and checklist (2026-10-23)

**Status: item 3 is partial, built behind disabled flags.**
`UPLOAD_WAVE_EXECUTION_ENABLED` stays default-off; `live_consumers_available()`
stays `False`. Nothing here changes current production behavior. This
continues the work in [upload-processing-generations-2026-10-23.md](upload-processing-generations-2026-10-23.md)
(step 2, model generations) and `docs/indexing-clustering-target-state.md`'s
step 3 ("make the wave protocol actually execute work"), which the prior
"18 passing tests" note meant literally -- 18 tests, not 18 implementation
steps. Real canonical-order clustering (`clustering_wave.py`) and the
publication obligation ledger (`wave_publication.py`) already existed,
fully tested, from an earlier pass, but neither was wired to a claimable,
renewable stage execution or a real queue consumer entry point. That wiring
is what this pass adds.

## Implemented

1. **Durable stage execution record** (`backend/execution_lease.py`,
   `ExecutionLeases`). Binds library/wave/stage/input-revision, a stable
   `executionId` across retries of the same revision, `status` (`running`/
   `succeeded`/`failed`), a renewable `owner`+monotonic `epoch`+`leaseUntil`,
   and a `progressCheckpoint`/`lastProgressAt`/`completionReceipt`. Kept
   explicitly distinct from `index_work.WorkStore`'s dispatcher claim (a
   send-in-flight reservation, not proof of completed work) and from the
   per-photo source-processing lease -- see the module's own docstring.
   Reclaiming an expired lease (including a same-owner reclaim) always
   mints a fresh `owner` and advances `epoch`; every mutating call
   (`renew`/`checkpoint`/`release`/`complete`/`fail`) re-validates
   `(owner, epoch)` against the current durable row first, so a stale
   holder's `finally` block can never release or complete its
   replacement's claim.

2. **Real queue delivery** (`upload_wave_execution.release_and_relay`).
   Reuses `WorkStore.request`/`notify`/`recover` as the dispatcher's durable
   relay (unchanged, already isolated from index-maintenance recovery --
   see `test_indexer_recovery_cannot_see_wave_stage_intents`), wrapping
   `dispatch_outbox`'s existing eligibility/CAS logic with the actual
   `notify(key, send)` call. All three handoffs in the target doc's table
   go through this one path: intake-closed->Vision and frozen-wave->cluster
   already existed; **assignment-committed->indexer is new** (see item 3
   below). `notify()` reserves its wake window before calling `send`, so a
   lost acknowledgement cannot cause a duplicate send within that window;
   a crash before any send attempt leaves the durable WorkStore request
   intact for `recover()` to retry.

3. **`ShadowWaves.commit_assignment`** (`upload_waves.py`), new. Commits the
   cluster consumer's handoff: `ready` -> `indexing`, binding the exact
   `requiredPublicationId` the cluster stage captured via
   `wave_publication.ObligationLedger.record_required`, and creating the
   `shadow-index!{waveId}` outbox in the **same transaction** as the wave
   row (same partition; the obligation manifest itself lives in a
   different partition and cannot share that transaction -- a crash
   between `record_required` and `commit_assignment` leaves the wave at
   `ready`, so the cluster consumer retries both idempotently). Extended
   `outbox_eligible`/`dispatch_outbox`/`advance_wave`/`sweep_library` for
   the new `index` destination, symmetric with `vision`/`cluster`.

4. **Vision consumer** (`run_vision_release`). Validates the release is
   still the exact eligible one (`claim_release`, tolerating an
   obsolete/duplicate wakeup as a silent no-op rather than an error), claims
   renewable Vision ownership, then drives `upload_wave_shadow.
   reconcile_page` (already using the corrected required-step/model
   contract via `processing_generation`, not legacy `processing_complete`/
   status counters) through exactly one full pass over the frozen
   membership, checkpointing between pages so a resumed attempt does not
   redo completed observation. Advances the barrier (`store.evaluate`)
   through exactly one full pass too; if members remain outstanding after
   that pass, this **releases** ownership (not fail, not complete) and
   returns -- a future wakeup or the recovery sweep re-claims rather than
   busy-looping on unchanged evidence. Once every member has qualified
   evidence or an allowed settlement, completes the lease and durably
   relays the cluster release in the **same call** -- individual photos or
   900-file upload chunks never independently trigger wave clustering,
   only this barrier does.

5. **Cluster consumer** (`run_cluster_release`). Claims one logical
   execution for the whole frozen wave, validates its immutable input
   receipts against the current wave row (`frozenRevision`/`manifestDigest`
   -- `outbox_eligible` already catches a stale `frozenRevision`, so this
   function's own check is specifically for `manifestDigest`, which
   eligibility doesn't look at), loads the frozen membership's faces,
   drives the existing `clustering_wave.run_wave_assignment` (unchanged;
   internal chunks and its decision journal stay implementation details of
   this one execution), then records the publication obligation set and
   calls `commit_assignment`. Never builds serving indexes. A
   duplicate/late release after the wave has already moved past `ready` is
   a tolerated no-op via the same eligibility check Vision uses.

6. **Index consumer** (`run_index_release`). Claims Index stage ownership
   for the exact committed assignment revision and projection obligations.
   Does **not** implement real publisher integration or receipt coverage --
   those don't exist yet. If the supplied `ObligationLedger` isn't complete,
   this fails the lease explicitly and leaves the wave at `indexing`; it
   never substitutes "the consumer ran" for publication proof.

7. **Failure-path tests**
   (`test_execution_lease.py`, `test_wave_execution_consumers.py`, 25 new
   tests): claim/renew/release/epoch semantics including a stale worker's
   `finally`-block release being rejected; crash-before-send retried by
   `recover()`; lost-ack/duplicate-relay dedup within the reservation
   window; duplicate/obsolete release tolerance at the consumer boundary
   for Vision and cluster; a zero-member wave advancing through the
   barrier via its own closed-session witness without any worker having
   been dispatched; a wave with real pending members releasing (not
   completing) ownership and a later attempt completing once the source
   settles; cluster input-receipt forgery rejection; cluster->indexer
   handoff commit and its duplicate-release tolerance; the index consumer
   leaving a wave at `indexing` rather than fabricating completion; and
   disabled-flag gating at both `dispatch_outbox` and `release_and_relay`.

## Remaining release gates

1. **Live cutover stays off.** `live_consumers_available()` is unchanged
   (`False`). Enabling `UPLOAD_WAVE_EXECUTION_ENABLED` only makes dispatch/
   consumer code paths reachable in tests and future manual invocation; it
   is not wired to any real queue trigger, worker process, or Azure
   deployment.
2. **No real queue trigger exists.** `release_and_relay`'s `send` callback
   and the consumer entry points are plain functions a future queue-trigger
   handler must call with real `azure.storage.queue`/Service Bus wiring,
   metadata/face table clients, a real `FaissAssigner`, and a real
   `ObligationLedger` table. None of that transport/host wiring exists yet.
3. **Real publishers don't exist.** The index consumer can only fail
   closed today -- there is no code that writes a real `ObligationLedger`
   receipt from an actual index-maintenance publication job. Until that
   exists, no wave can ever reach `complete` through this path.
4. **Recovery sweep has no scheduled trigger.** `sweep_library`/
   `WorkStore.recover()` are callable (manually, or from a future admin
   endpoint per the step-4-era plan) but nothing cron-schedules them yet.
5. **Vision's "process" step is observation, not model invocation.**
   `run_vision_release` barriers on already-persisted per-photo processing
   results (`reconcile_page`/`observe_source`); it does not itself invoke
   OCR/vision/face models. Actually triggering unprocessed photos' backend
   steps, if they are not already triggered by the existing per-photo
   upload pipeline independent of waves, is out of this pass's scope.
6. **One flag currently gates two independent behaviors**
   (`PROCESSING_GENERATION_ENFORCEMENT_ENABLED` from the step-2 pass gates
   both forced-reprocess supersession and the deployment-staleness check;
   `UPLOAD_WAVE_EXECUTION_ENABLED` here gates both outbox dispatch and
   every consumer entry point). No rollout sequencing between these has
   been decided.
7. **Azure deployment/live traffic validation** (real cron wiring, real
   queue triggers, live failure injection) requires an explicit deployment
   window and is deferred, consistent with "don't redeploy during active
   uploads."

## Validation

Focused: `PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python
-m pytest backend/tests/test_execution_lease.py
backend/tests/test_wave_execution_consumers.py
backend/tests/test_upload_wave_execution.py backend/tests/test_upload_waves.py
backend/tests/test_wave_publication.py backend/tests/test_wave_clustering_parity.py
backend/tests/test_upload_wave_shadow.py -q` -> 189 passed.

Full suite: `PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python
-m pytest backend/tests/ -q` -> **3,649 passed, 24 failed, 14 skipped, 28
setup errors** -- identical failure/error identities to the established
baseline (lexical parity fixtures, lexical rebuild concurrency, SQL
ordering, light-build dedup, live-FAISS setup errors); zero new
regressions. `git diff --check` clean.
