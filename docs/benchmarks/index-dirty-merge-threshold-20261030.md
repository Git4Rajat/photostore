# Index incremental-merge crossover: fixed fraction -> direct cost comparison (2026-10-30)

Analysis + local benchmark against the real `storage_utils` decision function
(`_dirty_set_too_large_to_merge`, `FakeTable`-backed, no Azure). No production
code change deployed from this doc; the resulting code change is tracked in
the normal diff/commit, not here.

## The finding

`_refresh_rows_index_on_disk` (sort/access indexes) and
`_merge_user_lexical_index_snapshot` (lexical index) both choose between two
paths once a dirty-filename set crosses `_INDEX_MERGE_MAX_DIRTY_ABS` (2,000):

- **merge**: one concurrent (16-way) point-read per dirty filename.
- **scan**: one paged `query_entities` sweep of the whole library partition.

The crossover between them was a fixed `_INDEX_MERGE_MAX_DIRTY_FRACTION` of
the library's row count -- originally `0.10`, assuming a bulk scan costs
roughly `L/1000` page fetches. LAW on this deployment
(`index.sort.table_scan`, 2026-10-30) measured the real cost at **~0.65ms per
scanned row**, not the ~0.01ms/row the 10% figure implicitly assumed. Against
the real 16-way point-read cost (~10.6ms/call, from
`vision_wave_attempt_summary.receipt_prefetch_point_ms`), the actual crossover
is far later than 10% of the library:

```
merge_ms(K) = ceil(K / 16) * 10.6
scan_ms(N)  = N * 0.65
crossover: merge_ms(K*) = scan_ms(N)  =>  K* ≈ 0.984 * N
```

Merge stays cheaper than a full scan up to ~98% of the library, not 10%. At
this library's size (~6,000 rows), every dirty-backlog size actually observed
in production LAW telemetry that day (deltas of 193-1,965 per wave,
accumulating across waves between build triggers to 2,000+) should have
merged under the correct economics, but the 10% fraction forced a full scan
on all of them.

## Benchmark

Ran the real `storage_utils.refresh_user_sort_index` decision path (mirrors
`tests/test_sort_index.py`'s `sort_ctx` fixture) with `_library_row_count`
mocked to the real library size, sweeping dirty-backlog sizes against a
6,000-row library:

| Backlog | Old (10% fraction) | New crossover model |
| --- | --- | --- |
| 193-1,965 | merge (below the 2,000 floor either way) | merge |
| 2,500 | **scan** (3,907ms) | **merge** (1,654ms, 2.4x cheaper) |
| 3,500-5,500 | **scan** (3,907ms) | **merge** (2,316-3,639ms, still cheaper) |
| 5,904+ (≈ full library) | scan | scan (correctly -- merge would now cost more) |

Runner: `backend/scripts/benchmark_index_dirty_merge.py` was not promoted;
the ad hoc version lives at
`/private/tmp/claude-502/.../scratchpad/benchmark_index_dirty_merge.py` for
this session and was not committed.

## Fix

Replaced the fixed fraction with a direct comparison of the two paths'
projected costs, using three measured, env-overridable rates
(`_INDEX_MERGE_POINT_READ_MS=10.6`, `_INDEX_MERGE_SCAN_MS_PER_ROW=0.65`,
`_INDEX_MERGE_CONCURRENCY=16`) instead of a fraction constant that has to be
hand-derived and re-derived as real per-op costs drift. `_INDEX_MERGE_MAX_DIRTY_ABS`
(2,000) is unchanged -- it's a correctly-sized cheap short-circuit below which
merge always wins regardless of library size, avoiding a manifest read.

**Update (2026-10-31):** the caveat below is resolved -- all five
`ThreadPoolExecutor(max_workers=min(16, ...))` call sites in
`storage_utils.py` now read `_INDEX_MERGE_CONCURRENCY` instead of a
hardcoded `16`, so retuning the env var changes real merge concurrency and
the decision math together.

~~One caveat intentionally left unresolved: `_INDEX_MERGE_CONCURRENCY` is
**not** wired to the real `ThreadPoolExecutor(max_workers=min(16, ...))` caps
in the actual merge code (five call sites in `storage_utils.py`, all
hardcoded `16`). Changing the env var retunes the decision math only; it
does not change real merge concurrency. Today both are `16`, so the model is
accurate, but this is a latent drift risk if either is ever retuned
independently.~~

**Recommendation:** keep the direct-cost-comparison model; if `point_read_ms`
or `scan_ms_per_row` drift (e.g. a storage account tier change, throttling),
retune the two rate constants rather than reintroducing a fraction.
