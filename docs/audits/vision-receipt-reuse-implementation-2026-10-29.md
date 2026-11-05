# Vision immutable receipt reuse and adaptive reads

Implemented for the existing source schedule and 2 CPU / 4 GiB deployment.
No model, infrastructure, retry-budget or browser changes are included.

## Behavior

- Each Vision source callback gets a private committed-receipt cache, bounded
  to 2 MiB of serialized strings/row allowances and 256 entries. It stores only
  immutable manifest/face rows after a successful create or fresh durable
  equality proof on conflict. Ambiguous/failed writes do not populate it.
- Capture and completion acceptance can reuse these rows across independently
  constructed `ArtifactReceipts` instances. Scope, hash, snapshot coverage and
  model-contract checks still run. Conflict validation bypasses caches.
- Successful source callbacks return immutable row snapshots to the coordinator.
  It imports matching table/library entries into the existing bounded receipt
  cache. Workers never share or mutate the coordinator's `LocalRows`. Failed
  callbacks do not hand off their cache, and every source scope clears on exit.
- Fresh-source qualification can skip fetching the just-written immutable rows.
  Mutable metadata, inboxes, policies, deletion/supersession proofs and leases
  are not cached by this mechanism. Existing ownership boundaries and fresh
  metadata checks remain in place. Overflow/misses fall back to durable reads.
- Remaining receipt prefetch starts with a query probe of at most 14 keys.
  An OR-query taking at least 100ms per bounded parallel query wave switches
  subsequent receipt fetches in that Vision attempt to four-way point reads.
  Singleton equalities do not count as scan probes; bulk fetch duration is
  normalized by query waves. State resets between attempts. Fast queries remain
  queries, avoiding a blanket regression for small partitions.

The 100ms strategy trigger is a latency heuristic, not a partition row-count
measurement or a proof of globally optimal selection. A transient slow query
can change the strategy for the remainder of one attempt. Existing metadata
and proof/inbox query strategies remain unchanged.

## Telemetry for the next upload

`receipt_commit_cache_hits/misses/evictions/oversize`, cache peak bytes/rows,
`receipt_commit_handoff_rows/bytes/ms`, `receipt_prefetch_cached_keys`,
`receipt_prefetch_query_groups/ms`, `receipt_prefetch_point_groups/ms`,
`receipt_prefetch_keys`, `receipt_prefetch_strategy_switches`, and
`batch_point_errors` are exported by the existing image/wave summaries.
Existing batch queue/service times, storage calls, qualification prefetch and
verification timings provide the comparison to total wall time.

Cache byte peaks are serialized budgets plus a per-row allowance, not total
Python/native RSS. Concurrent source intervals overlap. Handoff duration and
query/point durations must not be added as independent whole-wave savings.

## Verification

478 tests pass across receipt cache, receipt capture, table reads, source batches,
execution consumers, metrics, completion, fences, runtime, artifacts, filename
lookup, assignment files/journal and upload-wave shadow suites. Coverage includes
parallel isolation, durable conflict checks, missing/failed/ambiguous writes,
budget fallback, fresh-source races, failed-source handoff exclusion, unchanged
retry policy, small/large read selection and draining point reads on error.

No deployment or live upload result is claimed. Compare a fresh upload on the
same resource allocation, reporting qualification wall time, actual source
throughput, avoided receipt reads, fallback/switch counts and durable outcomes.
