# Vision apply/persistence call consolidation — 2026-10-27

The running upload exposed repeated source checks around immutable artifact
publication, two metadata rereads immediately after a successful result commit,
and a missing-inbox GET before creating each new completion reference. This
change removes those round trips without changing models or processing policy.

## Changed behavior

Promotion and receipt capture share an attempt-local immutable-work scope.
Their source checks run once at its successful exit, before metadata publication.
The scope contains create-only blobs/receipts and local result edits; mutable
face artifact writes retain their immediate ownership checks. Blob read-back,
JPEG/hash/ETag validation and face lookup consistency checks are unchanged.
Standalone publication/capture callers retain their existing fresh checks.
Recovery qualification uses the same scope before its result commit.

The metadata CAS now passes its returned service ETag and complete committed
outbox snapshot to the completion relay. Exact durable inbox acceptance is still
required. The common acknowledgement conditionally MERGEs only outbox/ack fields
using that ETag, with no metadata reread. A 412 triggers one fresh read and the
existing exact-ID acknowledgement logic; another conflict retains recoverable
outbox evidence. Missing/unsupported ETags retain the original two-read path.

New completion inbox records are created in a policy-fenced transaction without
an absence GET. A 409 reads and verifies the existing payload. Cleanup still
blocks new references, including when it races the transaction; exact existing
references remain acknowledgeable during cleanup, matching previous behavior.

## Verified request reduction

For two newly promoted prepared derivatives, receipt capture and a new completion:

| Operation | Previous point reads | New common path |
| --- | ---: | ---: |
| Source checks around promotion + capture | 4 | 1 |
| Metadata reads for completion acknowledgement | 2 | 0 |
| Inbox absence check | 1 | 0 |
| Total selected reads | 7 | 1 |

This removes five metadata GETs and one inbox GET in that case. It is not a
one-request apply pipeline: durable writes, fresh mutable-artifact checks,
manifest/snapshot qualification and blob verification remain. Existing verified
derivatives, duplicate deliveries, conflicts and clients without response ETags
have different counts. No full-upload wall-time saving is claimed before rollout.

## Instrumentation

- `artifact_source_checks`, `artifact_source_check_batches`,
  `artifact_source_checks_coalesced`
- `completion_ack_snapshot_uses`, `completion_ack_snapshot_conflicts`
- `completion_inbox_creates`, `completion_inbox_create_conflicts`
- `processing_apply_phase` phase `artifact_source_validation`, with its own
  context-local physical I/O, elapsed and calling-thread CPU counters

The final validation has its own phase instead of disappearing from accounting.
Compare total apply time and total physical requests, not only promotion/capture
phase durations, which now exclude that consolidated validation.

## Validation

Tests cover byte read-back retention, reduced actual client-call counts, stale
revision/owner/photo/generation/lease/deletion/isolation, immediate mutable-write
fencing, concurrent edits/new generations during delivery, invalid response
ETags, missing durable acceptance, lost inbox replies, immutable inbox identity
collisions and cleanup racing inbox creation. Integration coverage verifies the
result CAS's returned ETag reaches the acknowledgement path.

Validation result: **944 tests passed** across 46 related processing, wave,
cleanup, concurrency, receipt and diagnostics test files. `git diff --check`
also passed.

No Azure configuration, running image or application data is changed by this
implementation. Existing uncommitted BLAS infrastructure changes remain intact.
