# Clustering journal fsync — leverage, benchmark, recovery semantics (2026-10-30)

Analysis + local benchmark only. No production code, deployment, or
configuration changed. The clustering stage is already instrumented; this
documents where its time goes and de-risks the one biggest, safest lever.

## Per-step leverage (existing `cluster_wave_phase` telemetry, not new work)

Clustering already emits per-phase telemetry with per-phase IO deltas and
fsync counts. Fresh LAW for the clean 985-face wave `5e19459990b8` (phases are
inclusive wall ms; they overlap and must not be summed):

| Phase (step) | Wall | CPU | Dominant IO |
| --- | ---: | ---: | --- |
| `assignment_persistence` (materialize, 985 calls) | 43.9s | 10.6s | `other_table_write` 17.1s, `faceembeddings_read` 8.8s |
| `snapshot_prepare` (1 call) | 41.3s | 11.3s | **`photojobs_read` 27.9s**, `faceembeddings_read` 6.4s |
| `journal_write` (6,325 calls) | 32.7s | 7.5s | **fsync 22.9s across 1,972 fsyncs** |
| `curation_check` | 8.2s | 2.8s | |
| `matching` (real FAISS) | 4.1s | 4.1s | CPU-bound, small |

Three storage levers dominate; matching (the algorithm) is ~4s. This doc covers
the **journal fsync** lever (clearest and safest). The other two —
`snapshot_prepare`'s 27.9s of photojobs reads (receipt/point-read reuse) and
`assignment_persistence`'s per-face Table writes/embedding reads — are separate
follow-ups.

## The fsync lever

The mounted-file journal (`wave_assignment_files.FileAssignmentJournal`) is a
write-ahead log on the Azure Files checkpoint mount. During `prepare()` the
snapshot rows are buffered and flushed once (good). But in the per-face
**decision loop** (`preparing=False`), every record calls `_flush()` →
`os.fsync`: one for `decision!N` (before materialization) and one for
`applied!N` (after). That is **~2 fsyncs per face = 1,972 fsyncs** for this
wave, and Azure Files fsync is a network round trip: 22.9s / 1,972 ≈
**11.6 ms per fsync**.

## Benchmark (local, isolated journal)

`FileAssignmentJournal`'s decision-loop writes touch only the local file +
SQLite index (no Table/guard), so the flush path runs in isolation without
faiss or Azure. Local disk fsync is microseconds, so **wall here is not Azure
Files** — we measure fsync *count* and durable-record *parity*, then project
Azure wall from the LAW-measured 11.6 ms/fsync. 985 faces, page size 128:

| Mode | fsyncs | local wall | projected Azure journal-fsync wall |
| --- | ---: | ---: | ---: |
| current (flush per record) | 1,970 | 1.13s | **22.89s** (matches LAW) |
| batched (flush per page) | 16 | 0.04s | **0.19s** |

- **99.2% fewer fsyncs**; projected journal-fsync wall 22.9s → ~0.2s.
- **Output parity: the 1,970 durable journal records are byte-identical**
  between the two cadences — batching changes only *when* `os.fsync` is called,
  not what resume replays.
- The self-consistency check (1,970 × 11.6 ms = 22.9s = observed) confirms the
  projection method.

Runner: `backend/scripts/benchmark_cluster_journal.py`
(`PYTHONPATH=backend python scripts/benchmark_cluster_journal.py`).

## Recovery semantics — why naive batching is unsafe, and the safe reordering

The journal's invariant: **a decision record is durable (fsynced) before its
materialization**, so a materialized effect always has a replayable journal
entry. Resume rebuilds the private FAISS runtime from the frozen baseline and
replays *durable committed decisions in order* (hash-chained via
`previousDigest`); `_restore()` truncates a torn final append (an un-acked
write), and `applied!N` markers make materialization idempotent.

- **Unsafe:** materialize per face but fsync per page. A crash after
  materializing a face whose `decision!N` is not yet durable leaves an
  orphaned person row that resume will not replay → inconsistent assignments.
- **Safe reordering (what the benchmark models):** per page, (1) decide +
  journal `decision!N` for the whole page to the file buffer, (2) **fsync
  once**, (3) materialize the page + journal `applied!N`, (4) fsync once. The
  "decisions durable before any materialization in the page" invariant is
  preserved with 2 fsyncs/page instead of 2/face. Decisions stay sequential
  (each still sees the prior in-memory `_replay`); only the *fsync* is batched,
  and the in-memory decision order is unchanged, so the hash chain is identical.

Correctness gate: the existing **49 journal recovery tests**
(`test_wave_assignment_journal.py`, `test_wave_assignment_files.py`) — lost
replies, crash boundaries at every step, partial writes, resume-from-durable-
manifest, torn journals, deleted-person recovery — must stay green. They pass
today; a batching change is acceptable only if they still pass unchanged.

## Recommendation

The fsync lever is real (~23s/wave of the ~92.7s storage total), safe in the
reordered form, and benchmark-shown to preserve durable output. Implement it as
a page-batched flush in the decision loop that keeps the decision-durable-
before-materialize invariant, and gate it on the full 49-test recovery suite
plus a fresh-upload comparison (confirm `journal_fsyncs` drops from ~2/face to
~2/page and the `journal_write` phase collapses). Do not batch the fsync by
simply moving it past materialization.

Then the other two phases — but see below: both turn out far more constrained
than their headline numbers, so the journal fsync is the one clean clustering win.

## Benchmark 2 — `snapshot_prepare` photojobs reads (27.9s): already optimized / inherent

Grounded in LAW (one `snapshot_prepare` call, 2,601 SDK calls): the 27.9s of
photojobs reads is the wave's own inputs — ~985 immutable face-receipt point
reads plus completion inboxes — loaded via the shared `read_entities` /
`ReceiptBatch` path. That path **already applies the b32e661 adaptive
query→point strategy**: the same wave's summary shows `receipt_prefetch_point_ms
= 10.5s` (~985 face reads at ~11 ms) and `receipt_prefetch_query_ms = 2.1s`,
with a strategy switch already firing.

So the query-shape / receipt-reuse playbook from Vision is **already in effect
here**, and the remaining cost is the inherent price of loading the wave's faces
plus the existing-library baseline you must match against. There is no large,
safe, un-applied lever in this phase — point reads are already the right shape,
and you cannot match faces you have not loaded. Confirmed by reading
`_iter_wave_faces_cached` (uses `read_entities(..., workers=4)` + `ReceiptBatch`)
and the receipt-query-shape Azure results (point reads only win on large
partitions, which this already uses). **No code change recommended.**

## Benchmark 3 — `assignment_persistence` (44s): correctness-bound, not batchable

LAW: 985 calls, 4,437 SDK calls (~4.5 ops/face) — per face a live face read, a
fallback embedding read (`faceembeddings_read` 8.8s), a person read
(`other_table_read` 5.5s), and the person+member+face writes
(`other_table_write` 17.1s + `faces_write` 7.2s). The Vision batch/prefetch
techniques **do not transfer**, because clustering's per-face correctness model
requires exactly the pattern that defeats them:

- **Reads must be fresh.** `_apply` re-reads the live face/embedding/person to
  detect curation or source-replacement races *since the manifest froze*
  (`wave_assignment.py:319-350`). Bulk-prefetching them at loop start would make
  them stale and silently miss a mid-wave edit — a correctness regression, not a
  speedup. The embedding read is a fallback: `_embedding` uses the inlined vector
  when present and only hits `photofaceembeddings` when the face row lacks it.
- **Writes must be ordered.** Person → member → face, with the face stamp written
  last as the completion marker (`:338`, `:377`). Cross-face write batching
  breaks this per-face ordering and the resume model.
- **Hot-person serialization.** Clustering maps many faces to few persons, so a
  wave's writes serialize on a handful of person rows regardless of batching.

The only genuinely safe sub-levers are modest and conditional, not quick wins:
(a) combine the two **same-partition** writes — person and face are both in the
library partition — into one transaction (3 writes/face → 2), which needs its
own ordering/recovery proof against the 49-test suite; (b) eliminate the 8.8s
embedding fallback read by carrying the inlined embedding on the face row, a
schema change with its own migration/consistency implications.

**Recommendation:** do not pursue cross-face write batching — it fights the
recovery and curation-race guarantees. The journal fsync is the clean clustering
win; these two phases are mostly inherent or correctness-bound. If more is
needed after the fsync change, the same-partition person+face write combine is
the next candidate, gated on the full recovery suite — but it is a modest,
careful change, not a Vision-style batch.
