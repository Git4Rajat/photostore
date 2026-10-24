# Step 4: streamed inputs and per-face assignment decisions

Implemented in the opt-in cluster consumer. No deployment or flag enablement.
`live_consumers_available()` remains false. The old chunk runner is retained as
a legacy qualification harness; live wave execution uses `wave_assignment.py`.

## Execution and recovery

- Frozen source receipts supply one member page (at most 20) and one face
  snapshot at a time. SQLite holds input sorting and candidate/person maps on
  disk with a bounded page cache. Python does not collect the entire wave.
- Initial capture freezes the candidate baseline and needed person rows,
  including deterministic UUID5 person rows left by interrupted legacy writes.
  The manifest publishes only after all durable input/baseline rows exist.
  Resume validates counts/digests and loads this published manifest, not current
  source/face rows. An interrupted capture cannot silently adopt another
  baseline revision.
- The real legacy and wave paths share `FaissAssigner.choose_person`: identical
  threshold, margin, deterministic identity and candidate matching rules. Page
  size is not part of decision identity. Newly discovered identities remain
  candidates throughout the canonical pass.
- Each immutable face decision binds its input digest, canonical position,
  manifest ID and preceding decision digest. It is committed before any effect.
  Journal rows, applied markers and final coverage are conditionally fenced by
  the stage execution ETag in the same partition transaction.
- People, membership and face tables are recoverable materializations of that
  decision. Inserts are create-only; updates require genuine ETags. Source and
  ownership changes reject stale work; current person names are preserved.
  Person/membership effects precede the final face decision stamp. Lost replies
  therefore replay the same target without minting another person or overwriting
  newer curation. No cross-table atomic transaction is claimed.
- Each attempt builds a private runtime from its immutable baseline and replays
  recorded decisions in order. It never restores a legacy mutable checkpoint.
  Candidate state is keyed by face ID, so replay admits each candidate once.
  A crash discards local state; the durable decision chain remains authoritative.
- Applied-prefix progress updates are diagnostic checkpoints, never permission
  to skip missing decisions. A background stage heartbeat covers long native
  FAISS training/rebuild calls. Expiry/lost ownership rejects further effects.
- Complete coverage records the frozen manifest identity, exact face count and
  ordered decision digest. The wave binds that receipt when committing the
  indexer handoff. No metadata callbacks or serving-index builds run here.

## Limits and next work

The qualification fixtures prove equivalence with the single-pass oracle on
those inputs, not universal ANN recall. Production-size ANN quality, native
memory, first-capture I/O and storage require measurement. Initial capture
streams and stores the full assigned-candidate baseline; this is intentionally
explicit, not an incremental or free snapshot. Resume rebuilds/replays local
state rather than introducing another checkpoint protocol.

Existing per-row 60,000 UTF-16-byte and 128-face source-manifest limits still
fail closed. An incompatible source/person curation or changed partial-capture
baseline requires explicit settlement/recovery; it is never silently retargeted.
Retention/cleanup must preserve active `assignment!<wave>!` records until all
retry/publication references are gone. Legacy qualification, source-worker
adapter wiring, real publishers, scheduled recovery and measured app residency
remain release gates from the earlier audits. This work does not enable cutover.

## Validation

Final focused selection: **281 passed**. These tests cover real matcher oracle
parity, page-size changes, nonadjacent new identities, native delta compaction,
lost replies/crashes around decisions and materialization, no redecision on
replay, source replacement, curation preservation, orphaned deterministic
people, missing/duplicate inputs, baseline/policy changes, stage ownership,
heartbeat renewal, receipt binding and bounded input-object retention.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_assignment_journal.py \
  backend/tests/test_wave_execution_consumers.py \
  backend/tests/test_execution_lease.py \
  backend/tests/test_faiss_assignment.py \
  backend/tests/test_wave_clustering_parity.py \
  backend/tests/test_clustering_reference_oracle.py \
  backend/tests/test_clustering_runtime.py \
  backend/tests/test_clustering_checkpoint.py
```

The full backend run completed with **3,690 passed, 24 failed, 14 skipped and
28 setup errors**. All 52 failure/error node IDs exactly match the prior
artifact-regression baseline; no additions or removals. Final progress/receipt
assertions were subsequently covered by the focused command above; counts
across these overlapping runs must not be added.

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q backend/tests
```

`git diff --check` passed. No deployment, live flag enablement or live-cloud
residency test was performed.
