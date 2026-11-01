# Inline merge eligibility: library photo count is the wrong variable (2026-10-31)

Local benchmark (`backend/scripts/benchmark_index_incremental_merge_scale.py`,
FakeTable/FakeBlob doubles, no Azure) against the real
`storage_utils.refresh_user_people_index`/`refresh_user_albums_index`
ID-targeted merge path. No production code changed from this doc; the
resulting fix is tracked in the normal diff/commit, not here.

## The question

`inline_index_merge.py:_attempt()` gates every people/albums inline attempt on:

```python
if storage_utils._library_row_count(user_id) > _MAX_PREV_ROWS:  # 5000, photo count
    return
```

`_library_row_count` returns the search DB's photo count. But
`refresh_user_people_index`/`refresh_user_albums_index`'s ID-targeted merge
path never scans photos -- it point-reads only the affected people/albums
(already capped at <=100 IDs by `_people_merge_eligible`/`_albums_merge_eligible`),
then merges into the previous snapshot's row list. So: does merge cost
actually scale with *photo* count, with *affected-ID* count, or with
something else (the people/albums **index's own row count**, i.e. how many
named people / albums the user has)?

## Result

Swept both independently (5 samples each, p50 reported):

**Cost vs. index row count** (people/albums count), fixed affected=5:

| Index rows | People merge | Albums merge |
| --- | --- | --- |
| 100 | 0.56ms | 0.21ms |
| 1,000 | 1.96ms | 1.70ms |
| 5,000 | 8.40ms | 8.50ms |
| 20,000 | 34.51ms | 35.00ms |
| 50,000 | 92.72ms | 91.33ms |
| 100,000 | 195.78ms | 187.16ms |

Linear fit: **~1.95us/row (people)**, **~1.87us/row (albums)** -- clean O(P)
scaling, P = index row count.

**Cost vs. affected-ID count**, fixed index_rows=50,000 (people):

| Affected IDs | p50 |
| --- | --- |
| 1 | 91.57ms |
| 5 | 91.46ms |
| 20 | 93.07ms |
| 50 | 95.45ms |
| 100 | 98.36ms |

Flat. Moving from 1 to 100 affected IDs changes cost by ~7%, confirming the
dominant cost is the O(P) merge/re-sort of `prev_rows`
(`_incremental_people_index_snapshot_by_ids`/`_incremental_albums_index_snapshot`,
`storage_utils.py:7396-7416` and `:6652-6663`), not the point-reads.

## Conclusion

The flat `_MAX_PREV_ROWS` ceiling checks the wrong variable entirely:

1. **Wrong axis**: it reads photo count; real cost is driven by people/album
   *index row count* -- typically orders of magnitude smaller than the photo
   library (hundreds to low thousands of named people/albums, not hundreds
   of thousands of photos). A 500,000-photo library with 200 named people
   currently gets inline merge *blocked* by a library-size proxy, even
   though the actual merge would take ~0.4ms.
2. **Even on the right axis, the current cap is far too conservative**: at
   the measured ~2us/row, a 250,000-row people/albums index -- a scale no
   real user is near -- would merge in ~500ms, still inside a reasonable
   core-thread budget. The row count that would actually threaten a ~500ms
   budget on core's 0.5 vCPU is ~250,000, fifty times `_MAX_PREV_ROWS`'s
   current 5,000 (which was sized against photos, where 5,000 is a small
   library).
3. **Affected-ID count is already capped (<=100) and is not a meaningful
   cost driver** -- it doesn't need its own separate budget beyond what
   `_people_merge_eligible`/`_albums_merge_eligible` already enforce.

## Fix direction

Replace the `_library_row_count(user_id) > _MAX_PREV_ROWS` check with a
row-count check against the **snapshot `_eligible()` already fetched** (free
-- no extra I/O): `_people_merge_eligible`/`_albums_merge_eligible` already
load and return the full previous `LexicalIndexSnapshot` to validate
schema/version; `len(snapshot.rows)` is sitting right there. Cap against a
new, measured constant (e.g. `INLINE_INDEX_MERGE_MAX_INDEX_ROWS`, derived
from the ~2us/row rate and a real time budget) instead of re-fetching
`_library_row_count` at all. This makes eligibility depend only on the
incremental update's own cost (affected-ID count, already bounded, and now
the index's own row count) -- never on total library photo count, which is
exactly what the production behavior should be regardless of how many
photos a user has.
