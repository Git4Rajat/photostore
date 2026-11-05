# Large-upload finalization failures — 2026-10-24

User report: repeated 409 responses from the live session resolve route for
`02d90cda-9442-4cc7-a99a-da6df54a167f`, followed by browser logs containing
`finalize-batch` 500s and failed Blob PUT fetches. The selection declares 5,000
items and belongs to wave `f924cb177b0042308ecfbb850c6bce72`.

## Observed cause

Read-only dev logs establish the following sequence:

1. Parallel source admission/finalization competes on the library control row.
   `ShadowWaves._retry` exhausts eight short retries and raises
   `Conflict('Concurrent wave mutation; retry the request')` in the finalize
   after-request hook, while earlier entries in the batch may already be committed.
2. The exception handler returns `(jsonify(...), 503)`. Unlike a route handler,
   a Flask after-request hook must return a Response. Live traces report
   `AttributeError: 'tuple' object has no attribute 'headers'`, converting the
   intended retryable response into HTTP 500. Forty instances were counted in
   the queried interval.
3. Retrying finalization can rediscover the same persisted file in the hash
   index. A browser `duplicate` resolution can then disagree with its previously
   committed `accepted` outcome. Likewise, reporting `failed` after a lost batch
   response cannot replace an already committed success. Resolve logs show
   51-byte error bodies, matching `Resolved intake item cannot be rebound`.
   The source fence is rejecting contradictory outcomes as designed; removing
   that invariant would hide the original finalization error.

The pasted console also has failed PUT fetches with redacted URLs and no useful
response status. Those do not establish a Blob-service root cause, and the
patch below is not claimed to resolve every network/transfer failure.

## Local patch

- Return a real 503 Response and `Retry-After: 1` from the finalization hook.
- Reuse a same-hash finalization lease with at least 120 seconds remaining;
  refresh near expiry. Changed hashes and filenames still conflict. This avoids
  redundant control-row transactions between init, finalize and retries.
- Allow 16 bounded CAS attempts with capped 500ms jitter, retaining conditional
  atomic writes and explicit retry exhaustion. Maximum accumulated sleep is
  approximately four seconds, excluding storage calls; this is not a guarantee
  against arbitrary sustained contention or a throughput qualification.
- Preserve the committed successful intake classification on replay, while
  checking the full photo/revision/filename/required-step binding. Changed source
  revisions still fail; failed/cancelled items cannot be silently rebound.
- Treat actual finalization as an accepted source requiring projection, even
  when dedup finds identical bytes. Otherwise a file persisted before a partial
  batch failure, but not yet admitted, could be misclassified as a no-work
  duplicate on retry. No-write pre-upload duplicates retain the explicit
  duplicate-resolution path.

No live source rows, item outcomes, session states, policies or wave receipts
were modified. Existing failed items have not been declared successful or
repaired. Their recovery needs real source validation/retry, not synthetic
completion or forcibly closing intake.

## Verification

Two regressions failed on the previous implementation: the real Flask
finalization hook returned a tuple, and a successful same-source replay raised
an intake-rebinding conflict. Both pass with the patch. Added coverage also
checks a partial two-file batch followed by a successful HTTP retry, publication
requirements for both sources, healthy/expiring lease reuse, and success beyond
the prior eight-attempt budget. Existing exhaustion/fencing tests remain intact.

```bash
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave_live_routes.py backend/tests/test_wave_activation.py \
  backend/tests/test_upload_waves.py backend/tests/test_upload_wave_shadow.py \
  backend/tests/test_upload_wave_execution.py backend/tests/test_wave_recovery.py \
  backend/tests/test_wave_cleanup_live.py -q
# 164 passed

PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave*.py backend/tests/test_upload_wave*.py -q
# 304 passed

git diff --check
```

The broader 304 tests are local; the patched images have not been built,
published or deployed. A live browser upload at representative concurrency is
still required after rollout. The earlier four-user smoke did not cover this
5,000-item/browser-finalize contention shape.

## Current live state and rollout constraint

The final read-only check found the original session still open, 5,000 items
registered and 861 resolved. Its wave remains collecting, with 728 processing
members and three open sessions. These are observational reads during possible
activity, not a transactionally consistent whole-library snapshot. Vision must
remain gated until the real intake barrier closes.

Existing wave-owned libraries bind capability
`49cc07dae8347bc64bb3b29191d8746a6e2ac99c8dae28dbf52488223bc38764` to exact role
images. A new Upload image alone does not satisfy that certificate. Deployment
must include a validated, explicit compatible capability upgrade and coordinated
configuration/ownership handling for existing enrolled libraries. Do not label a
new image with the old deployment identity, silently rebind policies, patch live
container files, or restore legacy writers. That rollout and repair of the
current session have not been performed in this change.

Private diagnostics are in ignored `.azure/wave-enrollment-20261024/`, including
`upload-409-snapshot.json`, `upload-409-latest-state.json`, request summaries and
tracebacks. No diagnostic request sent email or changed user data.
