# Finalization retries preserve committed intake bindings

The reported live failure occurred after source finalization, when admission
rejected a different binding with `Resolved intake item cannot be rebound`.
The existing response hook treated this durable conflict as a retryable 503.
The report did not establish which identity field differed.

Code inspection found that already-resolved items still ran the full source
finalization path. That path resolves a filename again, can allocate a different
processing revision for a changed physical source, and resets processing status
fields even when the revision is retained. The response hook only checks the
immutable intake binding after those writes.

The live admission guard now verifies resolved items against their committed
accepted filename, photo identity, revision, and source hash before executing the
route. A supplied blob identity must also match. Single and batch finalization
reuse that verified result without calling source finalization, pending-blob
lookup, or rewriting source metadata. The hook keeps the committed processing
policy and finishes normal intake closure/handoff, including after a lost response
or a partially admitted batch. Unresolved batch members retain normal finalization.

Actual mismatches fail closed with HTTP 409, `wave_binding_conflict`, and the
names of differing fields. Identity values and file contents are not logged.
Transaction contention and storage failures retain the existing retryable path.
The transaction layer still rejects all attempts to rebind a resolved item.

Validation covers renamed filename replay, unchanged metadata/processing state,
closed-session replay, partial batches, changed photo/revision/hash rejection,
and the response hook's durable-conflict response. Relevant wave, finalization
fence, RAW preparation, and browser EXIF integration suites also run.

This change does not repair a source that was already replaced before deployment.
An affected live session requires read-only comparison of its saved item/member
binding and current metadata before any reconciliation. No deployment or live
session mutation was performed as part of this fix.

## Stop during admission failures

Stop previously only set a flag while retry backoff and offline waits remained
pending. The normal best-effort wave close also tried resolving locally unknown
items as cancelled, conflicting with an already accepted server binding. Local
recovery information could be cleared before closure was acknowledged.

Upload request retries now capture a cancellation signal per run. Stop rejects
queued/retrying calls and cancels their delay/offline waits immediately. An
already in-flight batched finalization is allowed to return its durable result;
its failure after Stop does not schedule another attempt. Transfer retries use
cancellable waits too. Stale queued jobs retain the old signal and cannot restart
when a later upload clears the mutable Stop flag.

Stopped runs wait for finalization results before artifact cleanup. They first
attempt normal closure, then use the existing server cancellation endpoint if a
live wave remains open. That endpoint preserves server-accepted members. The
browser clears its checkpoint only after acknowledged closure; failure preserves
recovery state and reports closure pending. Generation checks prevent delayed Stop
cleanup from retiring a replacement run's checkpoint.

Validation: 133 frontend tests passed across upload provider, wave sync, client,
and operation suites. Regression cases include offline/backoff cancellation,
no retry after Stop, lost-response binding conflicts, retaining checkpoints on
closure failure, and successful finalization arriving after Stop. TypeScript
checking passed. The earlier backend finalization suites passed 133 tests.
