# Reliability change audit — 2026-11-04

Scope: the uncommitted reliability changes following the 61-wave incident review, plus the execution-lease and dispatcher paths they depend on. This is a local code/recovery audit, not a new Azure operational-status check. No cloud configuration, queue or source data was changed.

## Findings and fixes

| Severity | Finding and concrete failure path | Resolution and regression evidence |
|---|---|---|
| **P1 — fixed** | An exception after a committed checkpoint still consumed the dispatcher failure budget. A productive clustering slice could apply another 128 faces and then fail copying its optional resume snapshot; repeating that sequence could exhaust dispatch despite actual progress. Normal successful slices already refunded this budget, but the exception path did not. | Shared `wave_runtime.consume` now uses the same durable-progress comparison on normal return and generic exceptions. Verified progress resets the dispatch budget while the original exception still propagates and its delivery is retained. No-progress errors still exhaust. A new test runs eight productive checkpoint-then-error attempts, followed by five stalled errors and terminal exhaustion. |
| **P2 — fixed** | Health reported only the wave row's progress age. Clustering/index checkpoints can advance while that row remains unchanged, so the new endpoint could falsely show a progressing stage as stalled. Also, identical checkpoint writes and same-input lease reclaim refreshed the execution progress timestamp without new work. | Health combines wave and execution checkpoint timestamps. Repeating the same checkpoint is now idempotent, lease heartbeats do not update progress, and reclaiming the same input revision preserves the previous progress timestamp. Tests cover all four cases and confirm a new checkpoint advances age correctly. |
| **P2 — fixed** | The final generic `DispatchAttemptsExhausted` capsule replaced the latest-error pointer, hiding the actual timeout/error from the health response. The immutable history survived, but the read-only diagnostic surface still lost the useful cause. | Keep separate latest-event and latest-processing-failure pointers. Health returns both, including whether each belongs to the current intent revision. The exhaustion regression preserves a prior `TimeoutError`/HTTP 503 and exposes it alongside the exhaustion marker. |
| **P2 — fixed** | The snapshot restore function caught `FileNotFoundError` around the entire restoration, including the final guard check. A later guard/storage failure could be treated as an optional-cache miss after the workspace/runtime had already loaded, leaking the runtime and attempting fallback against an already-populated workspace. | Only opening an absent optional workspace snapshot is treated as a cache miss. Later failures close the runtime and propagate. Added a guard-failure regression and a second-generation snapshot publication failure test: an old marker with a newer partially published snapshot safely falls back to the authoritative journal. |
| **P2 — hardened** | The general diagnostic label validator allowed filename-shaped strings such as `private.jpg` as error codes. Field allowlisting alone was weaker than the stated diagnostic-redaction contract. | Error-code fields now accept bounded lowercase code syntax, separately from deployment/revision labels. Filename/URI-shaped codes are rejected while `ocr_timeout` and `exif_parser_error` remain diagnosable. This rejects common payload shapes; syntax alone cannot establish the meaning of an arbitrary snake-case string. |

Source entry points: `backend/wave_runtime.py`, `backend/execution_lease.py`, `backend/index_work.py`, `backend/wave_health.py`, `backend/wave_assignment_resume.py`, `backend/processing_failures.py`. The exact-image release qualification now explicitly includes execution-lease regressions.

## Other audited boundaries

- Snapshot publication order: applied journal markers → closed workspace/native snapshot → resume marker. Missing, corrupt or stale acceleration state does not authorize a new baseline; missing/corrupt authoritative journals still fail closed.
- Restoration parity: canonical assignment output, foreground curation and source-generation replacement checks remain covered. Existing Table journals retain their original path. Old readers can ignore the additional local resume records and replay the authoritative decisions.
- Queue ownership: wave mode reserves one outer delivery while the shared photo pool retains configured concurrency. The flag-disabled legacy path retains batching. A mixed queue uses outer concurrency one when wave execution is enabled; this intentional tradeoff is documented.
- Failure capsules: create-only historical rows and the dispatcher pointer update share an ETag-fenced transaction. Diagnostic rows cannot be claimed as runnable work. Diagnostic persistence failure preserves the original processing exception and recovery path.
- Failure review: authenticated, bounded member pages; exact-generation comparison; cancelled/deleted/replaced source dispositions; no writes or automatic retries. Cursor validation rejects cross-wave and OData-injection-shaped cursors.
- Indexing: prepared/published scope reuse, receipt generation validation, interrupted final-marker pages and successor handoff tests remain in qualification. No forced barrier completion or retry-limit increase was introduced.
- Retention: zero grace is used only for the private per-wave checkpoint directory under its journal lock. Default shared-checkpoint retention remains unchanged. Tests verify current/recent generations and reader-lock protection.
- Deployment helper: preserves raw ARM metadata and identity, rejects incomplete projections, and produces an offline reviewable patch. It does not apply a patch or provide concurrency control for later manual application.

## Validation

The final broad run passed **860 tests**: wave runtime/continuations and intake, execution leases, snapshot journals, checkpoint retention, failure capsules/review, health, scaler patch generation, persistence, OCR, queue workers, work intents and fresh/upgrade deployment helpers. `git diff --check` passed, and Ruby YAML parsed the release workflow successfully.

One test-harness warning remains: a fake queue test replaces the shared `time.sleep`, and the background `perf-mem-sampler` thread receives the test's `_StopLoop` exception. The tests pass; this is not evidence of a production sampler failure. No Docker-image qualification or cloud fault injection was run locally.

## Remaining qualification and limitations

No unresolved P1 defect was found in the exercised paths after these fixes. That is not a certification of production behavior or all possible failures.

- The newly expanded release workflow must run against the built backend/Vision images. Local Python tests do not prove those images contain every required dependency or native runtime.
- Validate two eligible libraries on two replicas, deployment interruption and eventual scale-to-zero against live queues after rollout. No live fault injection was performed in this audit.
- Measure snapshot copy/restore latency, memory and storage traffic with large waves. Native rebuild/prefix replay is avoided on valid restoration, but full snapshot copying and journal restoration still have costs. Per-assignment storage-call optimization remains open.
- A whole-template scaler patch must be prepared from a fresh raw ARM snapshot and applied without concurrent configuration edits; it has no automatic stale-snapshot/ETag enforcement. The offline helper is not a deployment transaction.
- Native OCR calls are still bounded by the existing worker-pool policy, rather than hard process termination. Selective carry-forward receipts for repair generations and UI rendering of the new health details remain separate engineering work.
- Alert queries are supplied, but no Azure alert rule was deployed. Console logs alone cannot prove that an eligible stage has a runnable queue anchor.
