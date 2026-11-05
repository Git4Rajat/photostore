# Clustering algorithm compatible with the current wave protocol

Revision 2 — 2026-11-05. Status: compatible optimization design, with an initial subset implemented locally and not deployed. Source baseline: `ec52b307620e6bdb01963f9baf5d77c29b17be2a`. See the [implementation and measured call counts](benchmarks/clustering-compatible-optimization-20261105.md) for the exact completed scope.

**Decision:** retain the existing assignment authority, per-face live curation protocol, wave state machine and publication contracts. Use local artifacts to reduce preparation/replay overhead, not to replace authoritative person/face/member tables. The previous proposal's assignment-head control plane and snapshot-isolated override semantics are withdrawn from this revision.

The goal remains fast clustering for 1–100k incoming faces. This compatible revision targets redundant remote work; it does **not** promise zero remote calls per face. Eliminating the remaining materialization calls requires a separately qualified data-model migration. See the [speed audit](audits/clustering-speed-2026-11-05.md) for measured costs.

## 1. Compatibility decisions

| Area | Revised algorithm | Existing contract retained |
|---|---|---|
| Assignment authority | Materialize person, member and face rows; artifacts accelerate/recover that work | Indexers and APIs continue reading current tables |
| Input authority | Validate immutable generation-qualified receipts; optional cached bundles are derived views | Existing frozen wave, receipt verification and manifest digest |
| Curation | Read the live face, persist a curation witness when required, conditionally materialize before processing the next dependent face | `_curation_override`, `_apply`, genuine ETags and canonical order |
| Assignment receipt | Call existing journal completion and publish the existing compact header/complete rows | `assignmentReceiptId` and `commit_assignment` validation |
| Recovery | Existing authoritative journal plus optional validated workspace/native snapshots | Existing execution ownership, library lease, dispatcher, progress strings and continuations |
| Index handoff | Existing obligation capture, then atomic wave/outbox transition, then execution completion | Cross-partition idempotency; no invented distributed transaction |
| Empty/no-face work | Existing `advance_wave` / `wave_no_work` qualification | No fabricated empty assignment receipt or unnecessary FAISS initialization |
| Coverage tracking | Retain source admission/reservations; optimize them only through separately qualified scopes | Foreground mutation tracking and fail-closed coverage |

No new `LibraryHead`, overlay service, assignment-authority switch or wave state is required. Cluster still cannot build general catalog indexes. Keep the Vision → Cluster → Index sequence and predecessor-publication barriers.

## 2. Additional issues found during revision

1. **Obligations are cross-partition.** `ObligationLedger.record_required` uses `wave-obligations!<wave>`. The wave/control/index outbox use the wave store's partition. The former cannot join the latter's transaction. Capture the obligation set first, retry idempotently, and preserve `commit_assignment`'s existing atomic wave/outbox transition. `leases.complete` is another existing step after that transition.
2. **Authority cannot move without changing readers.** Index metadata preparation calls `_live_faiss_metadata_update`; People and representative-embedding builders read person/face/member tables. A result blob alone would leave those consumers stale. Revised artifacts are optional accelerators; they are not assignment completion proof.
3. **The supported input range is not an unconditional capacity guarantee.** Legacy `person.faceIds` and journal payloads have byte limits. A 100k-face wave concentrated on a single person may exceed them. Never truncate, split one person into several identities or skip faces to make the wave finish. Expose the existing failure through current diagnostics and treat membership-schema migration as separate work.
4. **Progress must mean durable work.** `faces-applied:<count>` participates in dispatcher progress/failure accounting and diagnostics. Downloaded inputs, speculative decisions, heartbeat changes and a successfully written optional cache must not advance it.
5. **Cached ETags are not fresh write authority.** A snapshot may store identity and source data, but its saved ETag cannot replace `_apply`'s live curation/person observations. Do not refresh a failed face ETag and blindly retry the old decision.
6. **Checkpoint granularity has existing limits.** The live assignment API accepts pages of 1–128; the default is 128. The previous 2,000-face checkpoint proposal does not fit that contract. Large artifact transfer chunks and small application/progress pages must be independent.
7. **A new owner must be able to resume old work.** Immutable decision/header binding excludes transient owners. Optional resume data is reusable across validated ownership changes, while every write uses the current owner/lease. Never modify an existing wave's algorithm/config binding in place to adopt this revision.
8. **FAISS state is more than vectors and a cursor.** Approximate-index training, slot revisions, delta contents and rebuild boundaries affect retrieval. Healthy resume restores the validated native/workspace state; any fallback uses the existing canonical journal replay path, not a newly captured live baseline.

These findings narrow the performance claim, rather than weakening wave correctness.

## 3. Existing records remain authoritative

Do not rename or redesign these records for the first implementation:

```text
Frozen wave:
    waveId, frozenRevision, manifestDigest, state, requiredPublicationId

Execution lease:
    existing ownership/input revision fields
    progressCheckpoint = "faces-applied:<count>"

Journal manifest:
    schemaVersion, binding, digest, faceCount, baselineCount, personCount

Binding:
    libraryId, waveId, frozenRevision, manifestDigest
    algorithm = canonical-faiss-v1
    threshold, margin, candidates, nprobe, deltaLimit, threads, indexConfig

Journal records:
    capture / input!N / baseline!ID / person!ID / manifest
    decision!N / curation!N / applied!N / resume!N / complete

Completion payload:
    waveId, libraryId, frozenRevision
    manifestId = digest(existing journal manifest)
    faceCount, decisionDigest

Assignment receipt:
    digest(existing completion payload)
```

Canonical order stays `(alignmentMethod, embeddingVersion, dimension, filename, faceId)`. Continue using the shared `choose_person` policy, including deterministic-person recovery, thresholds/margin, tier/version isolation and local distinct-person validation. Keep existing person creation metadata and decision serialization; do not replace the timestamp/UUID policy as an incidental performance change.

### Optional artifact envelope

A future preparation cache may contain serialized immutable input rows, ordered baseline/workspace data or a journal-index checkpoint. Its envelope must bind format/version, library/wave/frozen revision, input/journal manifest identity, source provenance, file lengths/digests and its covered journal prefix. It contains references to existing facts, not a second authoritative assignment result.

A cache is accepted only when exact coverage and provenance are validated. If validating a proposed bundle requires all the original per-face reads again, it has not established a read-saving fast path. Prefer the already validated journal snapshot on resume; upstream producer attestations/receipt bundles require their own qualification before replacing the existing loader. No Vision deployment change is required for the initial compatible revision.

Do not add optional cache fields to an existing immutable manifest or decision: that changes its digest and breaks receipt/replay identity. Store accelerators separately under a versioned private namespace or use the existing supported resume format. Old readers must continue to recover the original journal.

## 4. Stage orchestration algorithm

The following pseudocode specifies ordering and calls existing operations. It is not executable application code. New helper names describe proposed optimizations and require implementation/qualification; retained function names identify current contracts.

```python
def consume_cluster_wave(message):
    # Existing dispatcher owns the message, failure budget and handoff lifecycle.
    wave = advance_wave_using_existing_no_work_and_predecessor_rules(message)
    if wave.state != 'ready' or wave.zero_face_probe_pending:
        return existing_dispatcher_outcome(wave)

    return run_cluster_release_using_existing_protocol(
        message,
        assignment_runner=run_compatible_assignment,
    )
```

Inside `run_cluster_release`, retain `claim_release`, `execution_scope`, `_execution_check`, execution keepalive, and `AssignmentYield` handling. Default slice budget stays the current 120 seconds, checked at safe page boundaries. No new standing coordinator, periodic worker or additional per-photo queue is introduced.

A zero-face outcome must come from qualified processing evidence and the current no-work machinery. A missing receipt, failed fetch or incomplete probe is never evidence of no faces.

## 5. Preparation and resume algorithm

```python
def open_assignment(store, wave, assigner, check):
    binding = existing_binding(wave, assigner.config)
    acquire_existing_process_lock_and_library_assignment_lease()
    check()
    invalidate_existing_legacy_runtime_cache()

    journal = assignment_journal_using_existing_mode_selection(store, wave)
    # Marked mounted journals remain mounted. Old Table journals stay Table-backed.
    # A compact Table header from a local journal must not switch its authority.

    resumed = restore_existing_validated_resume_snapshot(journal, binding)
    if resumed is not None:
        header, workspace, runtime, next_position, chain = resumed
        return AssignmentState(header, workspace, runtime, next_position, chain)

    # Existing header → load original frozen inputs/baseline.
    # No header → capture exactly once, before assignment effects.
    header, workspace = journal.prepare(
        immutable_inputs=existing_generation_qualified_receipt_loader,
        baseline=existing_streamed_face_embedding_and_person_capture,
        binding=binding,
        baseline_revision=current_library_lease_checkpoint_revision,
    )
    runtime = build_existing_private_runtime_from_workspace(workspace)
    return AssignmentState(header, workspace, runtime, 0,
                           digest(['wave-decisions-v1', digest(header)]))
```

For a fresh run, stream into local SQLite with bounded Python memory, validate duplicate face/source coverage and preserve canonical order. For a small wave, process immediately; never wait for a larger batch to fill. A cold small wave may still require a large existing-library scan: do not assume a frozen baseline from another wave is compatible without a revision proof.

Preparation/recovery optimizations allowed after measurement:

- Larger immutable range fetches and bounded I/O concurrency, independent of the 128-face application boundary.
- Provenance-qualified receipt bundles; no cross-generation or live-row ETag reuse.
- A validated local journal-index checkpoint plus verified tail to avoid decoding the full JSONL journal on every healthy continuation. Never trust a copied SQLite index without proving its correspondence to the authoritative file prefix.
- Existing workspace/native resume snapshots, with explicit bytes/time/hit/miss telemetry. They already exist; do not implement a duplicate snapshot protocol.

## 6. Ordered decision and materialization algorithm

**Do not plan/persist the next dependent face before the current face's curation outcome is resolved and applied.** This preserves the existing curation chain and forbids the prior whole-page speculative decision algorithm.

```python
def run_compatible_assignment(state, journal, assigner, guards, progress, should_yield):
    page_size = 128                      # configurable only within existing 1..128
    assignment_started = False
    new_materialization_in_page = False

    for position, face in state.inputs_from_resume_position():
        guards.check()                  # current coalesced execution + library checks
        if position % page_size == 0:
            guards.boundary()           # force current durable eligibility check
            state.workspace.commit()

        decision = journal.decision(position)
        if decision is None:
            decision = existing_decide(face, state.workspace, state.runtime)
            bind_existing_manifest_position_previous_digest(decision, state)
            journal.put_decision_durably(position, decision)

        validate_existing_decision_input_and_chain(decision, face, state)
        observed = {}
        decision = existing_curation_override(
            journal, position, assigner, face, decision, guards, observed
        )
        identity = digest(decision)

        if not journal.applied(position, identity):
            if not assignment_started:
                library_guard.begin_assignment()   # invalidate durable baseline first
                assignment_started = True

            existing_apply(
                assigner, face, decision, identity, guards,
                observed_live_face_and_etag=observed,
            )
            # Person → membership → final face stamp, where outcome requires writes.
            # Excluded/preserved/curated cases keep their current special behavior.
            journal.put_applied_durably(position, identity)
            new_materialization_in_page = True

        existing_runtime_replay_once(state.workspace, state.runtime, decision, guards)
        state.chain = digest([state.chain, position, identity])
        state.next_position = position + 1

        if at_existing_page_boundary_or_end(state, page_size):
            progress(state.next_position)   # existing callback writes faces-applied:N
            if (new_materialization_in_page and not state.at_end and should_yield()):
                save_existing_optional_resume_snapshot(state, journal)
                raise AssignmentYield()
            new_materialization_in_page = False

    guards.check()
    return journal.finish(state.header, state.chain)
```

The progress callback receives the existing numeric count and writes `faces-applied:<count>` through `ExecutionLeases.checkpoint`; neither its signature nor the execution checkpoint format changes.

Curation/materialization constraints retained from current code:

- Curation witnesses bind the original decision, do not rewrite frozen inputs and cannot be introduced after a dependent next decision exists.
- Reuse the observed live face and its genuine ETag for the conditional face write. A later edit rejects it; do not reread merely to upgrade the ETag.
- Validate `SOURCE_FIELDS`, the frozen embedding and existing ownership. Keep the existing source-replacement errors and curation retry path.
- Read the current target person before updating it; preserve current name/other curation fields. Reject resurrection of a deleted matched person and collisions of deterministic new-person identities.
- Create/validate membership before the final stamped face write. A lost member/person/face reply replays the same durable decision; it does not recompute a new person choice.
- Call `_replay` only on the resolved decision, after current application handling. A `curated` override has no candidate; do not inject the overridden automatic candidate into later matching.
- The existing baseline is frozen for the logical run. This design does not promise stronger global interleaving semantics for unrelated baseline-person edits than current code provides.

No page-wide person/face transaction, blanket prefetch of mutable state, reordered canonical matching or whole-page journal group commit is approved by this algorithm.

## 7. Coverage and write-amplification reduction

There are two separate optimization gates:

**Gate A — wave-source context (implemented locally).** Establish a narrowly scoped `source_change` context only around validated wave assignment materialization. Coverage/scheduling regressions prove that ordinary foreground operations retain their behavior and the context resets on success/error. Keep source admission, begin/finish reservations, mutation tracking, ETags and lease checks. This removes redundant maintenance scheduling; it does not remove coverage traffic.

**Gate B — bounded coverage scope.** A later explicit API may consolidate repeated reservations and redundant source observations across a bounded assignment page. It is disabled until its proof and failure-path tests exist. It must retain the same source-write order and live curation checks; it cannot use raw clients as an untracked bypass.

```text
Before any source effect:
    validate operator admission + wave ownership
    reserve affected coverage using the existing coverage protocol
During effects:
    execute the ordered per-face algorithm unchanged
    retain exact old/new-owner tracking and genuine source ETags
Before finalization:
    ensure every potentially affected owner is covered before its source write
After effects or failure:
    finalize all successfully begun reservations using existing fail-closed rules
    preserve ambiguous/failed begin visibility and original exceptions
```

The affected-person set can change as live curation is observed. A page-start guess is insufficient. A scope must reserve new owners before their effects or use an explicitly supported conservative coverage mode, with bounds and semantics proven against current readers/builders. If this cannot be proven without equivalent calls, keep per-write tracking and optimize elsewhere. Do not roll back successful source writes when scope finalization fails; journaled recovery handles them.

## 8. Recovery, progress and shutdown

Retain the existing authoritative mounted journal or original Table journal for the lifetime of that wave. Decisions are durable before source writes; applied markers follow successful/no-op materialization. Existing torn-tail/digest validation and lost-reply recovery remain the authority.

The `faces-applied` count is the resolved journal prefix, including outcomes that validly require no source mutation. It is not the number of newly assigned faces or FAISS searches. Add new/replayed/curated/preserved/excluded telemetry separately; do not reinterpret existing counters.

```python
def yield_after_durable_page(state):
    # Ordered by existing APIs; none creates a final assignment receipt.
    record_existing_execution_progress(state.next_position)
    save_optional_closed_workspace_and_native_resume_snapshot(state)
    stop_execution_keepalive_then_release_ownership()
    return_to_existing_dispatcher_for_durable_continuation()
```

The current snapshot protocol saves workspace/native files and publishes `resume!N` last. The authoritative applied prefix remains recoverable if optional snapshot publication fails. Preserve productive-progress accounting on an exception after a checkpoint; no-progress attempts still exhaust normally. Do not manually reset attempts or mark every cache write as progress.

Restoration validates the original header/config binding, cursor, chain, workspace digest and native checkpoint identity/configuration. A valid hit skips earlier preparation/build/replay. Missing/stale/damaged optional state may use the existing canonical fallback. Share/ownership/storage I/O errors propagate; losing the marked authoritative journal fails closed. Never recapture a partially materialized library or skip an unapplied suffix based solely on a cursor.

Keep local active SQLite/FAISS files separate from the mounted share; publish only closed snapshots. Existing cross-process locks, identity/path rules and retention protection remain. A bare in-process cache is an optional optimization, not evidence of durable progress or current ownership. On shutdown, finish only a safe bounded unit; if interrupted earlier, replay the existing journal. The final page completes normally instead of scheduling an unnecessary extra slice.

## 9. Exact completion and Index handoff

Preserve the existing completion payload and hashing:

```python
def finalize_cluster_with_existing_protocol(store, wave, ownership, assignment_receipt):
    # The completed runner returned journal.finish(header, chain).
    # Mounted finish also published the compact Table header and complete proof.
    # assignment_receipt is the existing complete-payload digest, not a blob URI.

    scopes = existing_SCOPES
    graph = existing_PREREQUISITES
    ledger = ObligationLedger(
        existing_obligations_table, wave.id,
        library=store.library,
        frozen_revision=wave.frozenRevision,
        assignment_revision=assignment_receipt,
    )
    obligations = compute_obligations(
        store.library, wave.id, wave.frozenRevision, scopes,
        prerequisite_scopes=graph,
        assignment_revision=assignment_receipt,
    )
    frontier = ledger.record_required(obligations, prerequisite_scopes=graph)

    next_wave = store.commit_assignment(
        wave.id, frontier,
        manifest_digest=wave.manifestDigest,
        assignment_receipt=assignment_receipt,
    )
    leases.complete(ownership, frontier)
    return next_wave
```

This is **staged idempotent commit**, not a single atomic commit of blobs, obligations, execution, wave and queues:

| Interrupted after | Existing recovery requirement |
|---|---|
| Person/member write, before final face stamp | Replay the durable same decision; preserve prerequisite/curation checks |
| Final face stamp, before applied marker | Stamp proves prerequisites; idempotent `_apply`, then durable marker |
| Resolved prefix, before optional snapshot marker | Fall back to authoritative journal; do not adopt a newer unbound snapshot |
| Complete local journal, before compact Table header/complete | Retry `finish`; never rerun committed face effects |
| Partial obligation capture | `record_required` repairs the same complete immutable required set |
| Required set captured, before wave transition | Wave remains ready; retry the same assignment receipt/frontier |
| Wave/outbox transition, before `leases.complete` | Reconcile existing indexing state/outbox idempotently; do not fabricate a new receipt |
| Durable handoff, before queue send/ACK | Existing dispatcher/outbox retains runnable work; do not add a separate sender loop |
| Some Index outputs published | Verify and reuse exact existing generations/obligation receipts |

`commit_assignment` still validates the complete payload against its compact header, library/wave/frozen revision and face coverage; still enforces predecessor publication; and atomically transitions the wave to `indexing` with its index outbox in the existing partition. Only the existing Index completion protocol may mark the wave complete.

Index metadata preparation, scope dependencies, `assignmentReceiptId` checks, immutable output verification, completion-member pages and successor wakeups remain unchanged. Do not move person/member materialization into Index or let an optional cached assignment artifact satisfy publication obligations.

## 10. Capacity and request budget

Let `A` be newly materialized assignments, `N` all frozen input faces, `P = ceil(N / 128)` default application pages, `S` yielded slices, and `U` artifact transfer requests.

```text
Remote calls = immutable preparation + live curation/materialization
             + coverage tracking + journal durability + execution/dispatcher control
             + optional artifact transfers

Current materialization remains proportional to A:
    person write → membership write → face write for normal assigned outcomes
    plus current live reads/checks and conditional retries

Optimization goals:
    remove redundant scheduling and duplicate observations where proven safe
    avoid reconstructing the whole prefix on each healthy slice
    amortize immutable reads and bounded coverage where qualified
    measure U and snapshot bytes, not just Table call count
```

There is no compatible claim of O(P) total remote calls while keeping this per-face authority/curation contract. The local matching phase already avoids per-face remote search; persistence remains the main target. Keep per-record journal durability initially. Faster journal media or group commit is separate work requiring the curation/dependent-decision proof, not a tuning flag.

For 1/few faces, avoid unnecessary buffering, extra slices and full baseline recapture on valid resume. For 100k, use disk-backed input/workspace/runtime state, bounded fetch and native memory, explicit local/share disk budgets, and retained safe continuations. A large baseline, many compatibility groups, training or concentrated persons can dominate even if inputs fit.

Do not claim that 100k total faces implies support for 100k faces belonging to one person. Check the existing serialized membership and journal byte budgets before each durable decision/source effect, without silently changing current failure semantics. A clearer preflight/diagnostic can be added, but authoritative mid-run checks remain necessary.

## 11. Implementation order and qualification gates

1. **Measure current baseline:** image/config/journal mode, actual resume hits, newly applied versus replayed faces, wrapper/scheduling/coverage calls, journal open/save/restore and bytes. Historical replay timings are not current deployment measurements.
2. **Qualify source context:** keep ordinary foreground behavior and source admission; prove wave assignment schedules no standalone per-face maintenance. Reservation volume is measured separately.
3. **Improve healthy resume:** use the existing snapshots; optimize validated journal indexing/tail loading if measurements justify it. Preserve Table/local mode selection and fallback behavior.
4. **Improve immutable preparation:** larger bounded reads/cache bundles with exact provenance; keep order, generation evidence and freeze semantics unchanged.
5. **Qualify bounded coverage API:** only after the unknown-commit/paused-writer/owner-set proof. No untracked raw-client fast path.
6. **Evaluate deeper changes separately:** grouped materialization, journal group commit, scalable membership schema, or artifact assignment authority each require a new compatibility review and release qualification. None is implicitly authorized by this design.

Required acceptance cases:

- Frozen single-pass assignment oracle and decision/curation chain match current semantics under the same controlled edit schedule.
- Edits before decision, between decision and witness, between witness and conditional write, and after a prefix snapshot preserve current outcomes/errors; a rejected/curated automatic candidate does not leak into later decisions.
- Person rename, deletion/collision, source generation replacement, missing/changed embedding, rejected faces, preserved faces and no-work waves retain existing behavior.
- Lost replies and interruptions at every row/journal/checkpoint/handoff boundary resume safely with no fabricated completion.
- Native restore skips prior build/replay on a valid hit; optional damage replays the original authority and real I/O/lease failures remain errors.
- New owner resumes old binding; stale owner cannot advance durable work; cancelled/cleanup-fenced work remains fenced.
- Coverage unknown commits, finalization failure, orphan reservations and foreground dirty-owner tracking remain visible and recoverable by the current protocol.
- Same frozen wave retains all required publication scopes/prerequisites and exact assignment receipt linkage.
- Benchmark 1, 20, 1,902, 10k and 100k faces against empty/small/large baselines, with distributed/hot persons and native rebuilds. Report unsupported schema-capacity cases, not false success.

## 12. Source anchors and validation

- `backend/wave_runtime.py` — cluster/no-work dispatch, progress accounting, downstream metadata preparation and durable continuation.
- `backend/upload_wave_execution.py:755` — cluster release; `:824` — Index consumer and receipt/required-set checks.
- `backend/wave_assignment.py` — exact binding, ordered decision, live curation, materialization, runtime replay and completion.
- `backend/wave_assignment_files.py` — authoritative local journal, compact handoff proof and legacy mode selection.
- `backend/wave_assignment_resume.py` and `backend/clustering_checkpoint.py` — current resume identity/publication/recovery rules.
- `backend/upload_waves.py:561` — existing assignment handoff transaction and receipt validation.
- `backend/wave_publication.py` / `backend/wave_catalog.py` — cross-partition obligations, dependency graph and verified publication generations.
- `backend/faiss_assignment.py:638` — legacy membership size guard.
- `backend/automatic_index_updates.py:578` — tracked write/admission/coverage protocol.
- `backend/storage_utils.py:7979` — current People source-table consumer.

Design validation consisted of source-contract tracing, document consistency checks (including syntax parsing of all five Python pseudocode blocks) and **135 passing existing tests** across `test_wave_assignment_journal.py`, `test_wave_assignment_files.py`, `test_wave_execution_consumers.py`, `test_wave_publication.py`, `test_wave_continuation_safety.py` and `test_wave_health.py`. The subsequent initial implementation has a separate **412-test validation and added boundary checks**, documented in the linked implementation report. Deferred cache/coverage optimizations still require implementation-specific tests before release. No cloud data, queue or deployment has been changed.
