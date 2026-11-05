# Reliability changes from the wave incident audit

These source changes address the [61-wave audit](audits/wave-reliability-action-plan-2026-11-04.md). They have not been deployed by this change. Existing durable journals, generation fences, assignment semantics, publication barriers and zero minimum replicas remain authoritative.

## Vision

With `UPLOAD_WAVE_EXECUTION_ENABLED=true`, the queue loop reserves and executes one delivery at a time and disables legacy batch prefetch. The wave's source pool retains `IPWORKER_CONCURRENCY` parallel photo workers. This prevents a single replica from reserving multiple library waves and leaving another replica idle. It does not guarantee a particular replica identity or permit concurrent waves to bypass one library's ordering. In a mixed legacy/wave queue, outer delivery concurrency is also one; the flag-disabled legacy mode retains the original batching/concurrency.

OCR now retains `ocr_timeout` and distinguishes `ocr_capacity_exhausted` from an engine failure. These remain failed/retryable outcomes under the existing source budget; decode failure remains unrecoverable. This change does not increase budgets or claim a hard kill for native OCR calls.

Terminal source settlements now retain a bounded, allowlisted failure capsule inside the hashed immutable proof: attempts, generation, step statuses/error codes and exception class/status where available. Deployment revision and recording time are stored on the proof row. Arbitrary witness rows, exception messages, filenames and signed URLs are excluded. Existing proofs remain readable. This improves future failures; it cannot reconstruct diagnostic details discarded by older code.

## Clustering

Mounted-file journals now save optional acceleration state at a productive slice yield:

1. Commit the local SQLite workspace and create a local backup.
2. Save the native FAISS runtime using the existing checksum/configuration-validated checkpoint format.
3. Copy the closed workspace backup to the trusted mount, flush and atomically replace its snapshot.
4. Append/flush a journal resume marker bound to the manifest, applied-prefix position, decision chain and workspace checksum.

Restoration validates the local workspace copy and native checkpoint against that marker. A valid snapshot resumes at the next input without rebuilding the baseline index or replaying the prefix. A missing/stale/damaged optional snapshot falls back to the authoritative journal; a corrupt/missing authoritative journal still fails closed. Ownership and storage errors propagate rather than becoming cache misses. The wave journal mount lock serializes snapshot readers/writers. No active SQLite connection uses the mount.

Curation and source-generation checks still apply to the next unmaterialized face. Tests cover curation after a slice, source replacement, lost materialization replies, interrupted snapshot-marker publication, oracle parity and corrupted acceleration files. Private wave runtime snapshots explicitly retain the current/two recent complete generations with zero age grace; the shared library-checkpoint default retains its previous 24-hour grace.

The change eliminates repeated prefix reconstruction on a valid resume. It still copies snapshots and restores the append-only journal; it is not a claim of constant total slice cost or a measured production throughput improvement. Measure `resume_save`/`resume_restore` against the prior ~73-second snapshot/build overhead after rollout. Reducing per-assignment storage calls remains a separate profiling/optimization task.

## Indexing and shared runtime

Vision, cluster and index dispatch failures/no-progress exhaustion now retain immutable per-attempt capsules, fenced by the current dispatcher owner/ETag. A later successful intent deletion does not delete historical capsules. Terminal diagnostic rows are never runnable intents. Capsules preserve exception class, cause class, HTTP status, checkpoint phase/count and hashed cursors. Best-effort diagnostic failure logs retain the original processing failure and queue recovery path.

The live/shadow wave status GET now includes read-only `health`: current stage, dispatch failure/attempt count, last capsule, active execution ownership, predecessor blocking wave/state, Vision completion and search publication. It uses bounded point reads and does not wake workers, repair state or warm indexes. `dispatch_pending` means a durable intent exists; it does not certify that a queue delivery is present.

The existing prepare/publish/member-marker indexing continuation fix remains in place. Its scope reuse, generation validation, marker-page interruption and downstream handoff tests are included in qualification. No retry-budget increase or forced predecessor completion is introduced.

The authenticated `GET /upload/waves/live/<wave>/failures?after=<cursor>` endpoint provides a paginated, read-only per-member review manifest. It verifies immutable settlements and classifies the current source as matching generation, different generation, missing or deleted. Current diagnostic details are included only for a matching generation. Cancelled waves and operator-release records retain explicit dispositions. Follow `nextAfter` until empty, including pages that contain no failures; each page scans at most 20 members. No records are retried or mutated.

## Operations and release qualification

`deploy/queue_scale_patch.py` prepares an offline patch from a **raw ARM GET**. It preserves full queue metadata, authentication/identity, sibling rules and the rest of the template; rejects incomplete projections/unassigned identities/positive replica floors; and removes a stale revision suffix. It never contacts Azure. Example:

```sh
python deploy/queue_scale_patch.py --resource-file .azure/raw-app.json \
  --rule ipwork-queue --queue-length 1 --output .azure/queue-scale-patch.json
```

Use the actual rule name from the snapshot. Obtain a fresh raw snapshot before each change, review the before/after scale output, apply with the existing raw ARM deployment workflow, then verify KEDA events and actual processing/idle scale-down. Avoid concurrent configuration edits while applying a whole-template patch. The helper does not prove live scaler behavior.

`deploy/diagnostics/wave-attempt-failures.kql` provides a terminal-attempt event alert query. The alert must still be provisioned; no cloud alert was created here. The existing stall query remains available. Historical queue-anchor absence cannot be inferred solely from console logs.

The release workflow now runs the failure-capsule, checkpoint retention, scaler-patch and persistence checks in the exact backend image, plus queue/OCR checks in the exact Vision image before creating the qualified release contract. These workflow changes require a CI run; local tests do not substitute for built-image qualification.

## Validation and remaining work

Local broad regression: **794 passed**, with one test-only background sampler warning caused by the fake queue test replacing `time.sleep`. A final focused runtime/snapshot/curation/health/retention/scaler/deployment run passed **122 tests**. The final failure-review/runtime/route checks passed **76 tests**, including diagnostic-storage failure preserving the original processing exception. Newly added cases demonstrate that resumed slices skip prefix preparation/build/replay and still match the assignment oracle; publication interruption safely replays; edited sources are rejected and foreground curation is preserved.

Remaining operational qualification: build-image CI, controlled live rollout interruption, two-library/two-replica fairness and scale-to-zero, and production before/after latency/storage measurements. Remaining engineering work includes reducing assignment storage calls, selectively carrying successful receipts into a new repair generation, reviewing the historical failures using the new repair manifest, hard native OCR termination if resource measurements justify it, profiling/bounding individual long index publishes, UI rendering of the new health fields and deploying alerts. Existing failed waves are not silently revived and old source generations are not retried by this patch.

A subsequent [code audit](audits/wave-reliability-code-audit-2026-11-04.md) corrected productive-error retry accounting, progress-age reporting, exhaustion cause visibility and snapshot error handling, and tightened error-code redaction. Its final broad regression run passed **860 tests**.
