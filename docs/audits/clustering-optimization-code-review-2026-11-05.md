# Compatible clustering optimization audit — 2026-11-05

Scope: uncommitted changes to `wave_assignment.py`, `upload_wave_execution.py`, `processing_metrics.py` and their regressions, compared with HEAD `ec52b30`. Also traced source tracking, execution/dispatcher progress, authoritative journals/resume snapshots, publication obligations and the downstream maintenance handoff. This is a local code/fault audit, not an Azure deployment or throughput qualification.

## Result

**No new blocking correctness regressions identified in the audited patch.** Assignment ordering, curation/source-generation checks, conditional writes, coverage reservations, assignment receipt identity, obligation dependencies and Index handoff remain on their existing paths. This conclusion is bounded by the source review and tests below, not a proof of production performance or all possible interleavings.

No application logic was changed during this audit. Three additional fault regressions were added under `backend/tests/test_wave_clustering_optimization_audit.py`; the filename deliberately matches the existing image qualification command's `tests/test_wave*.py` glob. No cloud data, queue messages, deployment or git commit was changed.

## Audited boundaries

| Change/boundary | Finding and evidence |
|---|---|
| `_apply` source scope | The decorator surrounds materialization only. `SourceTable` still performs source admission and coverage begin/finish. Scheduling suppression additionally requires actual wave ownership; non-wave ownership retains scheduling. Source context is reset after normal return, a lost source reply, rejected admission and failed coverage finalization. Subsequent foreground writes still schedule normally. |
| Admission rejection | Added a real-runner/tracked-client regression: `SourceBusy` before a tracked mutation leaves person/face/member state unchanged, creates no coverage reservation and produces no final assignment proof. |
| Coverage finish failure | Added a regression where a source mutation succeeds but finalization fails. The original exception group propagates, the reservation remains active, coverage capture fails closed, no assignment completion proof is published and the source context resets. This intentionally retains existing orphan-reservation behavior; it does not silently certify success. |
| Foreground curation race | Added a tracked-client regression that edits a face after the old observation and immediately before the conditional assignment write. The old ETag fails; the confirmed manual owner survives, the assignment stamp is absent, coverage finalizes conservatively and no final assignment proof is published. The code does not reread solely to upgrade the failed ETag. |
| Larger frozen-member fetch | Fetch count is bounded to 20–1,000 and defaults to 1,000. Receipt validation groups remain at most 20. The extracted `_iter_wave_face_group` validation body was compared with the original body using normalized Python ASTs: identical. Generation/hash/face-count checks were not weakened. |
| Input ordering and boundaries | Real receipt fixtures produce identical ordered input snapshots for 20-row and 1,000-row fetches. The 41-member case changes member-list calls from three to one while retaining `[20,20,1]` validation groups. Boundary cases cover 0, 1, exactly 1,000 and 1,001 rows; invalid sizes fail before storage. |
| Replay-only checkpoint suppression | `_replay`, canonical chain validation and page-boundary eligibility checks still run. Only the redundant execution-progress write is skipped when the page introduced no new applied marker. Replay cannot repeatedly rewind `faces-applied` or masquerade as new progress. A completed duplicate delivery produces no new progress checkpoint. |
| Valid/invalid resume | Existing tests verify native/workspace restore skips prefix preparation/build/replay on a valid hit. Optional snapshot damage falls back to the original journal. Missing authoritative journals and real ownership/storage errors remain failures. Original bindings and Table/mounted journal authority selection are unchanged. |
| Productive versus stalled retries | Existing dispatcher continuation/failure regressions still pass. New applied markers, progress strings and wave transitions remain distinct from heartbeats, cache creation and replay. No retry-limit increase or failure-budget bypass was added. |
| Publication | No changes to `commit_assignment`, `compute_obligations`, obligation partitions, publication receipt verification or Index completion. Current staged idempotent handoff remains; no cross-partition atomicity claim is introduced. |
| Eventual maintenance | Completion still uses durable `index_maintenance`/successor handoffs. Independent scheduling is suppressed only for wave source writes; foreground maintenance retains its own path. The patch does not implement a new coverage-certification protocol or acknowledge outstanding reservations. |
| Telemetry | New labels are fixed, bounded counters registered in `_FIELDS`. `journal_open` timing is a separate inclusive phase. Counters distinguish replay from newly resolved outcomes; a resume miss includes a fresh execution without an acceleration snapshot. No filenames/embeddings enter new labels. |

## Validation

The combined local run passed **419 tests** across:

- Added optimization fault regressions.
- Assignment journals, local files, oracle parity, curation, lost replies and resume.
- Immutable source batching and large member-fetch boundaries.
- Wave consumers, publication obligations, continuations, health and runtime.
- Automatic coverage/source tracking and its runtime integration.
- Native checkpoints, wave activation and prepared derivative evidence.
- Processing metrics and diagnostic attribution.

The three new regressions were also selected by their release-gate-compatible filename. Syntax/diff checks and the AST comparison of the moved input validation passed. Docker image qualification, actual Azure request latency, shutdown fault injection on Azure and large-library RSS tests were not run.

## Remaining performance work and limits

1. **Per-face persistence and coverage calls remain.** Offline evidence shows scheduling calls drop from 512 to zero for the 128-face fixture, while coverage transactions remain 512. This is not near-zero total storage traffic and does not establish the earlier estimated throughput range.
2. **Member buffering has a row-count bound, not a new aggregate byte/RSS budget.** Up to 1,000 frozen member records are buffered in addition to the separately bounded receipt cache and matcher state. Measure large-library RSS and native training/snapshot working sets before claiming 100k-scale capacity.
3. **Healthy resume still opens/indexes the authoritative journal.** New timing exposes this cost but does not eliminate it. Journal-index checkpointing and coverage consolidation remain separately qualified follow-ups.
4. **Current membership and journal size limits remain.** The patch does not make 100k faces on one person supported, change the legacy `faceIds` representation or truncate oversized data.
5. **Coverage failures remain operationally significant.** An ambiguous/failed reservation finalization intentionally prevents certification; suppressing maintenance calls does not repair that condition. Retain the existing explicit recovery protocol.

The next performance check should compare identical frozen input/baseline on the same Azure resources and measure complete clustering elapsed, real WorkStore/queue requests, coverage traffic, new/replayed faces, resume rates, snapshot bytes and RSS. The [implementation report](../benchmarks/clustering-compatible-optimization-20261105.md) retains the precise measured-versus-projected distinction.
