# Vision apply-path optimizations

Implements the selected artifact and face/text benchmark results. Upload-response
receipts are **enabled by default**, as requested after the real Azure SDK tests.
The existing upload-session edits in this workspace are separate work.

## Runtime defaults and rollback

| Setting | Default | Effect |
|---|---|---|
| `IPWORK_ARTIFACT_UPLOAD_RESPONSE_ENABLED` | `true` | Successful create-only, content-validated upload ACK supplies receipt ETag |
| `IPWORK_ARTIFACT_PREVIEW_REUSE_ENABLED` | `true` | Reuse a bounded, decoded preview from the same source/revision |
| `IPWORK_FACE_PERSIST_BATCH_ENABLED` | `true` | Conditional per-table transactions for fenced multi-face writes |
| `IPWORK_SEMANTIC_TEXT_MODE` | `direct` | Set `batch` to opt into the broker |
| `IPWORK_SEMANTIC_TEXT_BATCH_SIZE` | `2` | Maximum texts per batch, permitted 1–8 |
| `IPWORK_SEMANTIC_TEXT_WINDOW_MS` | `5` | Collection window, permitted 0–50 ms |

Setting the first three flags to `false` restores their previous paths. Text
limit eight restores the previous broker size; direct encoding is a separate
Azure comparison option. No source-worker, image-batching or CPU limits change.
Effective settings are emitted with `vision_runtime_config`.

## Artifact publication

The worker's existing crop-input scope already spans inference and result apply.
It now binds retained preview bytes to the processing revision as well as the
photo/hash and scope's library/filename. Promotion reuses those bytes only for
the matching active source revision, and validates JPEG bounds as before. A
missing/stale buffer or disabled flag uses the existing bounded source GET.
Retention adds one reference to the existing compressed buffer, not a copy.

Create-only derivative upload requests `validate_content=True`. A successful
response with a nonempty, non-wildcard ETag permits a receipt constructed from
the upload ACK and locally validated bytes/hash/dimensions. No read-back GET is
required on that path. Existing-object conflicts or missing response ETags use
full bounded read-back and byte comparison. Transport errors propagate without
publishing a receipt; retries verify any object committed before a lost ACK.

Only proven immutable publication is remembered within the attempt, allowing
receipt capture to reuse that proof. The final source check and conditional
metadata publication remain in place. A stale attempt may leave orphan blobs
or local candidate fields but cannot use those as durable source completion.

Metrics: `artifact_upload_response_receipts`, `artifact_readback_fallbacks`,
`artifact_readback_bytes_avoided`, `artifact_preview_reuses`, and
`artifact_preview_bytes_avoided`. Existing artifact GET/verification/phase timers
remain available to compare the full wave.

## Face writes

Only source-fenced, non-destructive calls with multiple input faces collect
conditional writes. Zero/one-face and unfenced callers retain direct writes.
Per-table/per-partition transactions contain at most 100 unique row keys and a
conservative estimated payload below 3 MB. Original ETags or create-only
semantics are retained; duplicate pending keys and oversized rows are rejected.

Each transaction first validates/renews filename ownership, then checks the
current source fence. Face and embedding writes remain separate transactions;
the filename lookup is marked complete only after all queued writes succeed.
Failure clears the local buffer and follows existing dirty-generation recovery.
An embedding transaction can succeed before a later face transaction fails;
this is not cross-table atomicity. Concurrent curation causes an ETag failure,
never a retry that upgrades to a newer artifact ETag.

Metrics: `face_write_batches`, `face_write_batch_rows`,
`face_write_batch_ms`, `face_write_batch_errors`, plus existing source/lookup
reads and `face_persistence` phase timing. Batch errors count submit failures;
source/lease failures remain visible in the enclosing source/phase diagnostics.

## Semantic inference

Direct encoding is now the default: the first deployed upload produced 1,283
singleton batches and no pairs, accumulating 6.8 seconds of collection wait.
Opt-in batch mode retains limit two and a five-millisecond window; a full pair
flushes immediately. Per-source digests/results and the encoder's model lock are
unchanged. Batch mode remains available for controlled comparisons.

Additional metrics include total collection time, per-request queue time,
encoder service time, singleton batches and peak batch occupancy. Queue time
includes collection wait; do not add it to collection time or treat accumulated
parallel request time as wall time. Occupancy can be derived from total batch
items divided by batches. Lock/inference/tokenization counters remain distinct.

## Evidence

- [Local artifact experiment](../benchmarks/vision-artifacts-20261028.md)
- [Face and semantic experiments](../benchmarks/vision-face-semantic-20261028.md)
- [Real Azure receipt validation and verified cleanup](../benchmarks/vision-artifacts-azure-validation-20261028.md)

Production deployment and fresh-upload throughput validation remain pending.

## Follow-up integration fixes

The first real wave exposed missing integration coverage: the old per-photo
queue adapter opened crop/original input scopes, but `wave_runtime.process_source`
did not. The wave adapter now opens both around inference **and** result apply,
retaining only per-source references and cleaning them up on success or failure.
This enables the preview reuse already implemented in promotion and lets apply
reuse a fallback original instead of downloading it again. Existing identity,
revision, JPEG validation and publication fencing checks remain unchanged.

Reconciliation now raises a typed cancellation carrying `caller_cancelled` or
`scan_timeout`. When, and only when, the caller cancellation coincides with the
Vision stage deadline, the coordinator checkpoints the last processed cursor
and returns normally. No members from the fetched preparation page are skipped
or charged another source attempt. Existing runtime continuation scheduling then
refunds the healthy dispatch attempt and sends a normal short/immediate wake,
instead of waiting for five-minute queue redelivery. A scan's own 90-second
timeout, early caller cancellation, lease loss and other exceptions still fail.

`vision_deadline_yield` explicitly records the normal deadline transition.
Reconciliation timing logs include `cancellation_reason`. Semantic configuration
now uses scalar `semantic_text_mode`, `semantic_text_batch_size`, and
`semantic_text_window_ms` fields: the logger rejects nested dictionaries, so
the previous `semantic_text` dictionary was silently absent from telemetry.

Regression coverage uses the real wave source adapter for buffer retention and
cleanup, and the real stage coordinator/runtime for deadline cancellation,
unchanged pending member/scan cursor, dispatch accounting and retry enqueue.
It also verifies that early cancellation and genuine scan timeout still fail,
and that scalar configuration survives the actual telemetry logger.

Follow-up validation: **1,529 passed, one deselected** across the same 75 related
modules. The deselected case is the independently reproduced upload-wave
query-count assertion described below; the two previously failing baseline
modules remain outside this run. Final runtime/semantic focused checks:
**40 passed**. Changed modules compile and `git diff --check` passes.

## Validation

- Initial focused artifact, face, preview, wave/runtime and metrics suite:
  **249 passed**.
- Broader related suite (75 modules): **1,521 passed, one existing failure**.
  The failure is
  `test_upload_waves.py::test_scan_survives_store_restart_and_queries_are_bounded`:
  the pre-existing registration batching also records queries, while the test
  assumes every recorded query belongs to its one-row scan. Reproduced in an
  archive of committed HEAD with only the three pre-existing upload-session
  Python edits applied; none of these Vision changes were in that reproduction.
  The previously known baseline-failing `test_apply_persistence_phases.py` and
  `test_face_scan_timeouts.py` were not included in this related run.
- Final focused artifact/fence/semantic/optimization suite after added failure and
  100-row-boundary coverage: **119 passed**.
- Existing isolated artifact/face benchmark correctness tests: **20 passed**.
- Changed modules compile; `git diff --check` passes.

Tests cover default-on upload ACK proof, missing ETag/conflict read-back,
ambiguous success recovery, stale source and preview revision rejection,
multi-face transaction ordering, curation CAS rejection, partial cross-table
failure, 100-operation splits, context cleanup, rollback flags and semantic
configuration/occupancy metrics. The ETag-aware filename table double now
implements conditional create/update transactions rather than only dirty-index
upsert transactions.
