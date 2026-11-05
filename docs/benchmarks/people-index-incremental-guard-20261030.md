# People index incremental guard was unreachable, and index-maintenance false starts (2026-10-30)

Two bugs found while investigating why indexing cost stayed high despite
small, routine changes (person renames/merges/deletes, album edits).

## Bug 1: the people index's ID-targeted incremental path never ran

`refresh_user_people_index` (storage_utils.py) has an ID-targeted fast path
(`_incremental_people_index_snapshot_by_ids`) meant to rebuild only the
affected people instead of scanning the whole person/face table. It was
gated on two preconditions:

```
not previous_manifest.get('dirty')
and prev.source_version == str(previous_manifest.get('sourceVersion') or '')
```

Both were unconditionally false on every call, for the same root cause:
`touch_user_people_index_state` -- called by `_InvalidatingTableClient` on
**every** write to `person_table_client`/`face_table_client`, including the
very mutation that produces `affected_person_ids` and enqueues the rebuild
job -- replaced the whole manifest with `{dirty: True, sourceVersion: <fresh
timestamp>}`, discarding `rowCount`/`builtThrough`/`fullBuiltAt` and rotating
`sourceVersion` away from the published blob's version. So by the time the
queued job ran, both guard conditions failed regardless of how complete
`affected_person_ids` was. Every person rename/merge/delete took the full
`_build_user_people_index_snapshot` scan, every time.

A second, narrower gap fed into the same symptom: the soft-delete/restore
visibility path (`_set_faces_source_deleted_for_filenames`) touched
`person_table_client`/`face_table_client` and called
`touch_user_people_index_state`, but never called `mark_people_dirty` with
the people it touched -- so even if the manifest guard were fixed, those IDs
would be invisible to `affected_person_ids` (built from the ID-tracked dirty
set, `get_dirty_people`).

### Fix

1. `_set_faces_source_deleted_for_filenames` (app.py) now calls
   `mark_people_dirty(user_id, touched_people)` alongside
   `touch_user_people_index_state`, closing the one writer gap found.
2. `touch_user_people_index_state` (storage_utils.py) now preserves the
   existing manifest's `sourceVersion`/`rowCount`/`builtThrough`/`fullBuiltAt`
   when marking dirty, instead of replacing the whole document -- so the
   blob/manifest version-consistency check can still pass after a touch.
3. `refresh_user_people_index`'s ID-targeted tier no longer gates on the
   blanket `dirty` flag -- `affected_person_ids` (the live dirty-people set
   read fresh by the caller, `app.py`'s `_run_index_build_job`) is trusted on
   its own now that every writer marks its touched IDs. The schema/version
   match check (now meaningful again per fix 2) still applies. The
   timestamp-based second tier, which has no per-ID list, is untouched and
   stays gated on `dirty`.

## Bug 2: index-maintenance queue false starts (same shape as clustering)

`photostore-index-maintenance`'s KEDA rule uses `queueLengthStrategy: 'all'`
(needed so an actively-renewed in-flight job's invisible message isn't
mistaken for idle and SIGTERM-killed). Two delayed-send paths put
deliberately-scheduled (not actively-held) messages on that same queue:
`wave_runtime.send_stage` for `stage=='index'` continuations, and
`app._send_index_wakeup` (via `index_work.WorkStore`'s coalescing/retry-backoff,
used by `enqueue_index_build`). `'all'` can't distinguish the two invisible-
message cases, so every scheduled retry kept `queueLength>=1` for its whole
delay -- measured live over 14 days: 84% of `index-maintenance` replica
wake-ups (105,205 of 125,521 billed seconds) did zero index-build work.
Identical mechanism to the clustering false-start issue fixed earlier this
session (`docs/benchmarks/cluster-false-starts-20261030.md`), left there as
an explicit follow-up for index.

### Fix

Same shape as clustering: delayed sends from both paths now route to a new
sibling queue, `photostore-index-maintenance-deferred`, scaled with
`queueLengthStrategy: 'visibleonly'`. `run_index_worker` polls it as a second
tier after the main queue. Vision (`photostore-ipwork`) has the identical
exposure and remains an unshipped follow-up.
