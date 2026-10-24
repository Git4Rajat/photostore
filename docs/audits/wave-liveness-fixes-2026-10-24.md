# Wave continuation and recovery fixes — 2026-10-24

Continues the existing soft-deadline work in `app.py`, `wave_runtime.py` and
`upload_wave_execution.py`, plus the committed abandoned-session and failed-stage
recovery work. No cloud deployment, certificate rebind or live repair was run.

## Behavior

- Vision persists an exact member cursor and a separate evaluation phase. A
  yielded attempt releases its stage ownership and queues a delayed continuation
  before its old message is acknowledged. A completed scan still evaluates all
  evidence; unresolved earlier sources are revisited on the next complete pass.
- The worker watchdog observes source/page progress rather than aggregate wave
  runtime. Lease heartbeats do not reset it. Vision wave queue visibility renews
  on the polling thread, and acknowledgment uses the newest pop receipt.
- Recorded source-step failures consume the individual source retry budget.
  Ownership, source identity and unrecorded storage/receipt failures still fail
  closed. Two failing sources can each reach five attempts without consuming
  the entire stage budget on the first source.
- Paged zero-face probes continue through the Cluster queue without loading
  FAISS. Index completion immediately promotes and wakes the direct successor;
  control-only completion also tries that handoff.
- Session cleanup has a durable survey cursor and resumes an already-started
  abandonment page. Completing abandonment immediately advances its wave.
  Failed-dispatch cooldown starts at failure time, with compatibility for rows
  predating the failure timestamp.
- The deployment template restores five-minute scheduled recovery, in addition
  to event-driven nudges and explicit continuations. This is a bounded job, not
  an always-running coordinator. Fresh-deployment verification requires that
  schedule, and the opt-in recovery-heartbeat alert is restored.
- Foreground face curation of an unapplied decision is preserved through a
  separate immutable witness in the assignment journal. Its digest participates
  in the decision chain. Already-applied decisions are not rewritten, and
  changed source identities or embeddings cannot be admitted as curation.
- Before freezing Vision evidence, tag-index invalidation is explicitly restored
  and read-verified. An older clean tag snapshot cannot hide newly uploaded tags.
  The model-owned tag artifact still rebuilds through Vision's existing sweep;
  it is not an additional synchronous index-worker publication obligation.

## Validation

579 tests passed, including new regressions for deadline continuation, source and
readiness cursor resumption, 20-member zero-face waves, successor dispatch,
independent source retries, abandonment over 20 items, session history over 40
rows, failure cooldown, curation before and after an interrupted decision, tag
freshness and renewed queue receipts. One background sampler warning occurred
because the visibility-timeout test replaces the process-wide sleep function.

```sh
PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest \
-p no:cacheprovider -q backend/tests/test_wave*.py backend/tests/test_upload_wave*.py \
backend/tests/test_processing*.py backend/tests/test_fresh_wave*.py \
backend/tests/test_index_work.py backend/tests/test_tag_embedding_index.py \
backend/tests/test_ipworker_face_batch.py backend/tests/test_ipworker_max_retries.py \
backend/tests/test_ipworker_visibility_timeout.py backend/tests/test_ipworker_graceful_shutdown.py \
backend/tests/test_index_publication.py backend/tests/test_clustering_reference_oracle.py \
backend/tests/test_upload_init_batch.py backend/tests/test_finalize_batch_browser_exif_e2e.py \
--tb=short --show-capture=no
```

`git diff --check` passes. Local Bicep compilation of `deploy/sandbox.bicep`
succeeds; warnings remain for storage API type availability, minimum name length,
and nullable workspace references. This is not provider validation or real Azure
upload qualification. The existing exact-image capability ownership and upgrade
restriction remains in force; no upgrade or recertification mechanism was added.
