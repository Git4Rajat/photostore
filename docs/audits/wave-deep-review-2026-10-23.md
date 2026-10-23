# Deeper wave-protocol review — 23 October 2026

## Outcome

The target is more explicit after executable exploration, but remains a design
contract, not a production-ready implementation. This pass added an abstract
transition model, an actual-source idle-wakeup reproduction and additional
implementation requirements. No application implementation or Azure resource
was changed. Existing unrelated workspace edits were left untouched.

## Executable transition exploration

Run:

```sh
.venv-photostore-tools/bin/python docs/audits/wave_protocol_model.py
```

Saved output: `wave_protocol_model_results.json`. The standard-library model
explores all reachable states in three bounded scenarios:

| Scenario | Reachable states | Transitions | Safety / recoverability |
| --- | ---: | ---: | --- |
| Two ordered waves, optional intake reopen, per-member fan-out and two index receipts | 2,744 | 3,828 | Pass |
| Two initially empty waves, skipped processing/clustering/indexing | 22 | 28 | Pass |
| No processing members but outstanding clustering cleanup, followed by normal work | 644 | 856 | Pass |
| Total, counted separately per scenario | 3,410 | 4,712 | No unrecoverable states in these scenarios |

Invariants require one library slot owner, no successor advancement before
predecessor completion, no processing with open intake, settled members before
clustering, all required receipts before completion and no unnecessary claims
for empty stages. Source work already dispatched may settle while intake is
reopened. The root dispatch-controller claim is distinct from source work.

Ten deliberate contract removals are detected (two added in the follow-up
ownership/coverage review):

| Mutation | Counterexample |
| --- | --- |
| Lose Vision outbox recovery | Intake closes but processing never becomes runnable |
| Accept stale Vision claim | Intake reopens, then an old message advances processing |
| Allow stale slot release | Completed predecessor clears the successor's active slot |
| Lose final completion evaluator | All publication receipts exist but wave/successor cannot finish |
| Lose fan-out resumption | Root wake is acknowledged before member messages are dispatched |
| Retain old dispatch-controller claim on intake reopen | Reclosed intake cannot resume its new dispatch frontier |
| Accept one index receipt as complete | Publication stage closes before all required projections |
| Release the library slot after assignment | Ownership is released while predecessor publication remains active |
| Fabricate an assignment no-work outcome | A skipped result hides nonempty assignment/cleanup obligations |
| Accept a foreign-wave receipt by scope | A receipt from another wave is credited to the current wave |

The first model draft exposed the retained-controller-claim problem. The
document now explicitly requires quiescing/releasing/superseding that claim
separately from valid per-photo leases; recovery must finish this if a crash
interrupts it. This is an abstract protocol omission, not a newly reproduced
production defect.

**Proof limits:** edges model intended durable atomic steps and crash windows
between them; they do not establish that the real database operations implement
those steps atomically. The model checks safety and `AG EF complete`: every
reachable state has some legal recovery/completion path. It does not prove
eventual completion under an unfair scheduler. It bounds waves/members/reopens
and queue losses, represents receipt identities as two bits, and does not
model real source ETags, worker threads, Blob mutations, authentication, actual
clock skew, lease durations, source replacement/deletion, curation conflicts,
multi-library performance or scaler behavior. Those remain integration and
fault-injection obligations. Deliberately bad variants reproducing failures
prove that the checks detect those omissions, not completeness of all checks.

## Concrete source-backed sleep blocker: push negotiation

Run:

```sh
node docs/audits/index_ready_idle_reproduction.cjs
```

The reproduction transpiles and executes the real
`frontend/src/services/indexReadyPush.ts` using mocked transport, authenticated
online state and deterministic timers. No input events occur and no real
network call is made.

- Push unavailable: seven Tools negotiation calls across six simulated minutes
  (initial call plus one each minute).
- Push available: three negotiations and three refresh callbacks across ten
  simulated minutes because the socket is deliberately closed after four
  minutes and reconnects.
- Explicit stop cancels later requests in both cases.

This is additional to job-status polling. The app mounts `startIndexReadyPush`
from `AppServicesProvider` without an inactivity policy. Therefore recommending
push as an automatic cure for idle polling was incomplete: negotiation,
unavailable-feature retries and reconnect/catch-up callbacks must all respect
the same activity/residency contract. Gate by authenticated foreground demand;
avoid periodic disabled-feature negotiation while inactive. When the user
returns, perform an authorized catch-up without discarding pending edits.

These observations establish timer-driven HTTP behavior locally. They do not
measure production app replicas, costs or real browser timer throttling.

## Other contracts tightened or left explicitly open

1. **Backend capability:** browser-only processing cannot complete autonomously
   once the browser closes. The guarantee must be restricted to qualified
   backend-capable modes or expose an explicit unsupported/browser-required
   state. Browser upload preparation is a separate activity.
2. **Stale retries and retirement:** an obsolete epoch wake cannot poison the
   current revision's retry budget. Terminal receipts/retirement frontiers must
   outlive delayed deliveries; library deletion/restore must invalidate old
   ownership and lineage.
3. **Recovery scan cost:** at most 100 returned rows does not prove at most 100
   rows of server-side work. Load-test property-filtered recovery against a
   large historical ledger and choose a partition/frontier strategy accordingly.
4. **Gallery visibility while indexing is deferred:** the design guarantees
   existing published browsing and honest stage status. It does not yet select
   how newly accepted, unindexed items appear after navigation or to another
   library member. Decide whether to expose a bounded source overlay or explicit
   pending-publication view; do not silently make accepted photos disappear or
   trigger a full background build merely on gallery return.

## Additional existing tests

134 backend tests passed for clustering leases, index publication, index events
and processing sweep eligibility:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_clustering_lease.py \
  backend/tests/test_index_publication.py \
  backend/tests/test_index_events.py \
  backend/tests/test_ipwork_sweep.py -q
```

Four existing frontend push tests also passed:

```sh
# From frontend
npm test -- src/services/indexReadyPush.test.ts
```

Those tests preserve current behavior; they do not enforce the proposed idle
policy or stronger wave/source fencing. The separate reproduction intentionally
asserts current wakeup behavior as evidence of the gap.

## Implementation-readiness gate

Executable-oracle follow-up: `backend/tests/clustering_reference_oracle.py`
now supplies `run_single_pass_reference`, an uninterrupted canonical-order loop
over the real matcher with no batch/restart orchestration. Twenty new tests in
`backend/tests/test_clustering_reference_oracle.py` passed: 16 combinations of
four chunk sizes with warm/checkpoint/cold/replay execution, plus four partial
chunk failures at person/member/face commit and metadata projection. Actual
ownership, membership, representatives, preserved names and projected-file sets
are compared to the reference; fixtures independently assert expected identity
relations, rejected-face behavior and model separation. Checkpoint cases verify
there was no silent cold source scan instead of restore.

These fixtures use small exact/flat FAISS groups and fault-injectable tables.
They do not qualify approximate IVF-PQ recall, real cloud source fencing, or the
future wave decision journal. A reference using the same matcher is independent
of chunk orchestration, not an independent proof that matching thresholds are
correct. The release oracle must be extended to the actual wave API and larger
compatibility/candidate cases, with explicit qualification of intended changes.

Follow-up ownership/runtime/coverage review: 175 existing runtime, checkpoint and
FAISS assignment tests passed. The runtime already enforces unique logical face
IDs and revision-filters obsolete native slots; raw duplicate insertion is not
an established current defect. The target now separately requires bounded native
replay growth, journaled assignment decisions, checkpoint/source-frontier
reconciliation and canonical-order chunk equivalence. These are release gates,
not guarantees obtained from the abstract model. The model represents wave
receipt origins and typed no-work outcomes; it does not implement/test real
hashed obligation descriptors, source ETags or ANN decision-journal replay.

Exact 175-test selection, run from the repository root:

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_clustering_runtime.py \
  backend/tests/test_clustering_checkpoint.py \
  backend/tests/test_faiss_assignment.py -q
```

The count excludes the wider cold-stream, incremental-assignment, face-ID and
live-app suites. Adding `backend/tests/test_clustering_reference_oracle.py` to
this command is the separately reported combined 195-test selection.

Conclude the target discussion only with an explicit remaining decision list:
actual storage/partition schema and source fencing; immutable Blob acceptance;
held versus runnable foreground/wave projection work; automated recovery job
configuration; and gallery visibility/capacity budgets. The subsequent full-role
inventory in target-document section 2 selects separate bounded scheduled
execution for the automated scanner, sharing protocol code with the existing
Recovery HTTP app rather than hosting a periodic scanner inside that app.
Use the model's counterexample traces
as integration-test inputs when those choices are implemented. A design review
cannot responsibly claim no further gaps solely because the bounded model passes.
