# Upload face-index creation boundary: optimization blocked

## Current contract (2026-10-03)

No fresh-upload empty-index initializer is implemented. A missing filename-face
index remains **unknown**, never authoritative zero. The first face store for a
missing index still queries the face table, including for recently initiated
uploads. This query filters on filename but Table Storage has no filename
secondary index, so its cost can grow with the library partition.

Only a schema-valid `complete` generation with `faceIds: []` permits query-free
zero handling. `_begin_face_filename_write` acquires/invalidates the generation;
`_store_client_face_generation` queries unknown/dirty generations and preserves
rejections and curation; completion publishes authoritative IDs using ETag CAS.
Partial failures remain dirty or writing, requiring recovery rather than being
reclassified as a fresh upload.

## Why current upload signals cannot establish freshness

- Single and batch upload initiation write tracking onto a **reusable logical
  filename**. They reset `upload_started_at` on existing metadata. A recent
  timestamp (including a 24-hour window) is never evidence of zero faces.
- `pendingAnonymousBlob` / `anonymousImageId` UUIDs allocate **physical blobs**,
  not face namespaces. Face lookup and face rows still use the logical filename.
  A new blob UUID can coexist with an old rejected face under that name.
- `get_or_create_metadata` is a read-or-default helper, not conditional metadata
  allocation. It treats transport errors as absence and returns defaults.
  Even a real successful metadata create would not exclude orphan face rows.
- `_claim_filename_owner` conditionally creates a `(filename, library)` row,
  but its Boolean result also accepts same-content retries, deleted-name reuse,
  and transport failures. Even the successful create branch cannot exclude
  prior faces after ownership cleanup. This is a collision index, not a durable
  never-reused namespace registry or an exclusive upload-generation lock.
- Hash deduplication reports matches; it does not lock or fence face writers.
  Collision renaming uses eight UUID hex characters, includes paths without an
  atomic owner claim, and falls back to the original name on failure. None of
  these resolver outcomes carries safe freshness evidence.
- Finalization reuses metadata and upserts processing state before gallery index
  invalidation and route-level client-result application/enqueue. Initiation
  already publishes metadata for early thumbnail claims. Late-result application
  only checks whether the current metadata is deleted: it does not validate an
  immutable asset generation against the upload/result sender. A delayed result
  from an old upload can therefore target a subsequently reused logical name.
- Hard deletion now retains metadata and filename ownership until its leased
  face cascade succeeds. Query/page, source-delete, embedding, and membership
  failures propagate and leave acquired lookup generations incomplete rather
  than publishing a false complete-zero. This closes the normal hard-delete
  release ordering gap, but does not repair legacy orphan faces or turn the
  collision owner into an exclusive asset-generation fence. Incomplete-upload
  cancellation removes tracking metadata without a face cascade. Ownership/
  metadata absence therefore still cannot prove no stale faces. Existing
  deletion-produced complete-zero rows must not be overwritten or treated as
  fresh-allocation evidence either.

Adding an initializer to initiation, metadata creation, or finalization using
these signals would invent a proof the current protocol does not provide.
`_set_face_ids_for_filename(..., [])` is also unsuitable as an initializer: it
acquires/replaces existing generations and can overwrite a populated complete
row. A prospective initializer must use **create only**, never that setter or
upsert, and must never overwrite writing, dirty, legacy, rejected, or complete
rows.

## Concrete safe alternative (not implemented)

Introduce a server-allocated, full-random immutable **logical asset filename**
at initiation, separate from the user-facing original filename and physical
blob UUID. Merely changing the blob name or adding a timestamp marker is not
this protocol. The entire lifecycle needs the following contract:

1. Persist an idempotent upload-session-to-asset allocation using conditional
   creation and durable CAS ownership. Retries resume that exact allocation;
   ambiguous transport outcomes are read back, not assumed fresh.
2. Use a server-only namespace that client-supplied filenames cannot claim.
   Keep non-reuse reservations/tombstones through cancellation, deletion,
   retention cleanup, and reupload. Reuploads allocate a different asset key.
   Legacy logical filenames do not acquire freshness merely by looking random.
3. While allocation is exclusively held and before metadata, SAS/claim access,
   gallery visibility, or processing publication, conditionally create the
   complete-zero filename index. Existing index rows are never overwritten;
   collision or unexpected state aborts/abandons that allocation. On an
   ambiguous create outcome, only a read-back matching the allocation's own
   initialization token can permit publication; other rows fail closed. No
   accessible metadata may exist until this prerequisite has succeeded.
4. Publication follows a durable allocation state machine. Failure before
   publication leaves an inaccessible reservation, not a reusable name or a
   published false-zero marker. Once publication may have occurred, retry must
   **never recreate a missing index**: use the ordinary authoritative-query
   recovery. A missing marker after processing is not fresh again.
5. Carry/check the asset identity in finalization, browser early claims/results,
   queue payloads, and workers. Delayed uploads/results address only their old
   immutable asset, not a replacement using the same display filename. Update
   cleanup, delete, restore, dedup, and frontend retry flows accordingly.
6. Keep existing ETag generation leases, partial-write recovery, rejection and
   curation handling unchanged. The optimization only supplies the initial
   authoritative zero for the new allocated key.

This is a cross-lifecycle protocol change, not a safe local timestamp shortcut.
Until it exists, retaining the first query is the safe behavior. Querying once
under a face-generation lease at allocation could establish authoritative IDs,
but would **move**, not eliminate, the growing filename-query cost; without
asset fencing it also would not establish immutable fresh-upload ownership.

## Regression coverage

`backend/tests/test_upload_face_creation_boundary.py` exercises the actual
tracking helpers and finalize path: recent single/batch initiation cannot erase
missing/dirty/complete/writing generations; successful owner creation and newly
published metadata do not hide rejected orphan faces; duplicate finalization
preserves indexes; ownership release/reclaim still reconciles old faces; a
complete zero is query-free while missing-index upload initialization is not.

Existing filename-lookup tests cover concurrent acquisition, old-writer CAS
fencing, rejection/curation, transport failures, and dirty partial-write retry.
No claim is made that fresh-upload first-face queries have been eliminated.

## Cleanup hardening (2026-10-03)

Single/batch face cleanup acquires the filename generations before authoritative
enumeration and keeps them through shadow/source mutations and empty publication.
Only genuine not-found deletes are idempotent success; other failures propagate.
Metadata/ownership remain as a retry anchor if the hard-delete cascade fails,
and the caller receives a cleanup error rather than deletion success. Group
lease renewal is periodic with forced validation at scan boundaries; source
mutations renew their own key, avoiding quadratic renewal IO across a batch.
Cleanup fails closed if the lookup coordination table is unconfigured.

Dedupe's discovery scan is advisory. It acquires affected filename generations,
rescans under those leases, performs mutations with propagated failures, and
publishes the full surviving face set from another authoritative scan. Missing
legacy lookup rows are no longer skipped. Any failure leaves acquired generations
dirty (or writing if release itself fails), requiring fresh reconciliation.

`backend/tests/test_face_cleanup_safety.py` covers scan/page/delete/shadow failures,
lease acquisition/expiry, ownership-release ordering and hard-delete retry,
partial publication, bounded renewal IO, and missing-index dedupe recovery.
This does not introduce immutable asset IDs, fence every curation/legacy writer,
or claim cross-table transactional guarantees: the existing lease protocol and
its limitations remain. Late results for reused names still require the full
asset-identity change described above.