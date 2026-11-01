# Additive incremental index upgrades

## Scope

Existing full/bulk build algorithms and wave publication remain supported.
Incremental updates are an additional path, not a replacement. This handoff was
verified against current code on **2026-11-01**; automatic source coverage,
scheduling, summary refresh and immutable-artifact cleanup are integrated.
This documentation update changes no code or infrastructure and performs no
deployment; it does not establish which versions are running in production.

## Operational compatibility: cleanup now requires operator quiescence

**When durable updates are active, existing library/account cleanup and even a
normal library-content wipe require explicit proof that ALL library writers
are stopped. The existing emailed approvals/cleanup UX alone are insufficient.**
Wave ownership, an idle coverage counter or expired worker leases do not prove
quiescence. Unproved cleanup fails closed rather than deleting automatic state.
Stop/drain upload, vision, clustering, maintenance/full builders, legacy
processes and paused HTTP requests; keep them stopped through recovery/purge.

The authenticated admin route `POST /api/admin/indexes/source-reservations`
implements the operator lifecycle (see [backend/routes/admin.py](../backend/routes/admin.py)):

1. `action=fence` with a nonempty `reason` arms an indefinite library admission
   fence and returns `fenceId`.
2. After externally stopping/draining **all** writers, send `action=prove`,
   that `fenceId`, a nonempty `reason`,
   `confirm=ALL_LIBRARY_WRITERS_STOPPED`, `allWritersStopped=true`,
   `noPausedRequests=true` and `legacyWritersStopped=true`. The route checks the
   real renewable catalog guard and absence of live maintenance/model claims;
   these checks supplement, not replace, the operator's external proof.
3. For orphan repair, `action=recover` with `fenceId` persists each rebuild
   intent before resetting reservations to `fullRequired`, preserving dirty IDs.
   For cleanup, run the existing approved purge/wipe while the proof is live;
   immutable artifacts and automatic state are purged before cleanup completion,
   with proof rechecks and retryable failures.
4. Explicitly send `action=resume` with `fenceId` when recovery/purge has finished
   and it is safe to release admission. Recovery persists rebuild intents;
   fenced builders can process those intents after resume. Partial failures
   leave the fence in place and must be retried, not bypassed.

**Orphan source reservations and the operator fence/proof have no timeout.**
A SIGKILL or partially completed reservation fails closed until explicit
operator recovery; elapsed time must never hide a paused source writer.

## Activation

[backend/storage_utils.py](../backend/storage_utils.py) derives the module-level
`DURABLE_INDEX_UPDATES` boolean at import time from
`bool(os.getenv('INDEX_MAINTENANCE_QUEUE_NAME', '').strip())`. A nonempty queue
name enables it; an absent/blank name disables it. **There is no independent
`DURABLE_INDEX_UPDATES` environment-variable opt-in.** Existing jobs, queue and
`searchdirty` storage are reused; no new infrastructure is required by this path.
Configured storage and scheduling hooks must actually be available: failures
retain evidence/retry work or fail the source operation, not silently certify it.

## Implemented

| Index | Added or strengthened facility | Current activation |
| --- | --- | --- |
| Search SQLite | Bounded dirty captures, continuation, original-version acknowledgement | Existing search maintenance |
| Sort/access | Strict incremental-only merges: defer instead of full-scan fallback | Existing inline path, subject to ownership/base/budget checks |
| People/albums | Strict incremental-only merges, membership budgets, artifact CAS; album search dependency checks | Existing inline path, subject to real catalog serialization |
| Lexical JSON | Streamed gzip merge with bounded changed rows and disk uniqueness table | Automatic certified filenames through existing maintenance work |
| Photo vector | Streamed NPZ merge, SQLite row mappings, immutable data/manifest CAS | Automatic certified filenames through existing maintenance work |
| People embeddings | Streamed NPZ merge and targeted person/face reads | Automatic old/new owner coverage and configured model identity |
| Tag embeddings | SQLite photo-tag contributions/refcounts; only newly required tags encoded | Automatic full sidecar seed/targeted maintenance, **vision owner only** |
| Upload hash snapshot | Streamed hash-map merge and manifest/delete-epoch transaction CAS | Automatic certified hashes through the dedup maintenance scope |
| Explore/SQLite timeline | Exact reduced-row memberships and UTC day aggregates inside search SQLite | Atomic delta transactions; worker refresh on every successful drain, including noop |
| Raw-stream summaries | Standalone ordered contribution sidecar | Explicit utility only; distinct from the integrated SQLite reference |
| FAISS | Existing incremental delta retained | Unchanged |

The strict artifact paths return lightweight publication receipts rather than
hydrated library-sized snapshots. Existing ordinary/full callers retain their
snapshot contracts. Failure to publish cannot count as successful maintenance.

### Automatic coverage and recovery

[backend/automatic_index_updates.py](../backend/automatic_index_updates.py)
tracks all source tables for its five artifact kinds: metadata, photo embeddings,
face embeddings, faces, people and hash rows. `SourceTable` wrappers are installed
at client construction, inside cache proxies, so raw/batched persistence,
including FAISS writes, follows the same reservation path. Face changes include
old and new owners; uncertain/racy source reads force full coverage rather than
guessing a complete affected set.

Before source storage, writers persist WorkStore intent, active reservations and
versioned dirty IDs; after storage they advance coverage again and reschedule.
Controls/dirty IDs reuse `searchdirty`; jobs reuse the existing `WorkStore` ledger
and queue recovery. Tag work uses the existing jobs table's model-tag pending
partition and is consumed only by the vision process, not by core/index-worker
loading the model. Guarded waves retain their declared outputs/receipts and force
an automatic coverage seed after wave completion.

The worker captures an idle source revision plus **at most 512 dirty IDs** with
bounded lookahead. Missing/uncertified bases, truncated captures, uncertain
coverage, incompatible artifacts or model mismatch select the retained full
builders for bootstrap/bulk/repair. A strict targeted deferral persists
`fullRequired` for the next durable retry, rather than hiding a publication
failure with a second build in the same attempt. Full builds remain full-source
work; the 512 bound limits dirty capture, not total full-build cost.

Publication is followed by source-revision/ETag CAS certification, then original
dirty-ETag acknowledgements. Full fallback drains overflow acknowledgements in
bounded batches while the captured revision remains idle. Queue-send/post-write
scheduler failures leave durable intent or uncovered revisions for existing
queue recovery; they do not justify dropping dirty work.

### Universal publication lock and immutable GC

All catalog publishers, including full/legacy, wave, hash and model-tag paths,
share the **real renewable library catalog lock** from base capture through
pointer commit; same-library nested publishers reuse the live guard. GC acquires
that same lock **after successful WorkStore completion and claim release**, only
in `APP_ROLE=index-worker`, not from readers or ordinary HTTP handlers.

The existing 60-second worker recovery scan also retries GC for quiet libraries
after grace expires. Certified hash controls select one candidate per library;
bounded rotating windows prevent deferred libraries from starving later ones.
The default cap is **25 libraries per recovery**, configurable with
`INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY`. No new queue, timer resource,
maintenance scope or metadata-wide scan is required. Operator-fenced libraries
and active waves are skipped; cleanup failures do not block queue wakeups.

[backend/index_artifact_cleanup.py](../backend/index_artifact_cleanup.py)
defaults to **24-hour grace, current references plus the newest previous
generation, and at most 100 blob deletions per pass**. Discovery is paged,
SHA-library-prefix scoped and bounded (default 256 items/page, 100-page survey
cap); an incomplete survey defers before deletion. Grace applies to both artifact
age and the latest manifest/pointer update as a conservative retirement time.
Frequently updated manifests can therefore defer GC. Fixed aliases and wave
projections are not immutable GC candidates.

GC requires all **five** controls idle, fully certified and matching current
manifest lineage, and no live maintenance/model-tag claims. It rechecks control
ETags, references and the guard before conditional candidate deletion. Unknown
or missing evidence defers; a pointer commit alone is not coverage certification.
Grace, retention and deletion caps are configurable via
`INDEX_ARTIFACT_CLEANUP_GRACE_SECONDS`,
`INDEX_ARTIFACT_CLEANUP_RETAIN_GENERATIONS` and
`INDEX_ARTIFACT_CLEANUP_MAX_DELETIONS`. Readers must finish within the configured
grace; indefinitely paused readers are not protected by this retention policy.

## Important cost distinction

Vector, people-embedding, lexical and hash upgrades avoid whole-index Python
collections, and target source reads to affected records. They still stream
the existing artifact and rewrite a compatible complete output. Artifact CPU,
disk and network work therefore remains proportional to artifact size, even
for one changed record. They are bounded-RAM upgrades, not constant-time delta
publication. The existing search database already publishes small deltas;
other artifact formats would need a reader-compatible overlay for that property.

Memory includes fixed SQLite caches, streaming buffers and capped changed
records. Oversized IDs, memberships, metadata rows, incomplete captures,
incompatible bases and transient read failures defer/abort. Disk requirements
include base download, intermediate state and output, and must be budgeted.
Model-dependent tag inference stays with the existing model owner.

## Correctness contracts

- Explicit affected sets must include **every** relevant change since the
  expected base, including both people for reassignment and all deletions.
- A capture marked `has_more` or `truncated` cannot be certified complete.
- Source mutations and publishers require appropriate serialization/fencing.
  Manifest CAS alone does not prove that an affected set covers source writes.
- Dirty acknowledgements use the captured ETags. Concurrent re-marks survive.
- Incremental-only attempts never bootstrap or silently scan the full source.
  The durable worker remains the backstop.
- Lexical snapshot versions are separate from shared catalog coverage;
  isolated JSON upgrades do not falsely advance Listing/SQLite readiness.
- Immutable blob pointers are user-scoped; failed pointer CAS does not activate
   uploaded bytes, and successful pointer commits still require source certification.
- Full publishers remain compatible with the wave BuildGuard's declared outputs.
- Source writes and blob/table publication are not one atomic transaction.
  A source writer overlapping publication may leave a **temporarily stale,
  uncertified pointer visible to readers**. Certification loses the CAS, no
  dirty versions are acknowledged, and the next worker rejects that base and
  performs a full repair. This is not a linearizable read guarantee.

## Remaining limitations

1. **Every running source writer must use this version and its tracked clients.**
   Mixed-version/legacy processes or external raw table writers bypass the proof;
   a publisher lock cannot repair untracked source coverage. Verify all writers,
   not only the maintenance worker, before relying on automatic certification.
2. Deployment-scale sizing remains unverified: measure RSS, CPU, local disk,
   source reads, artifact bytes and latency on deployment-size machines.
   Passing tests is not million-photo sizing certification or a live distributed
   concurrency/load validation. Whole-artifact rewrites/downloads still scale
   with artifact size.
3. Summaries preserve two different full references, not one interchangeable
   representation. Indexed SQLite summaries are automatic; the standalone raw
   sidecar still requires explicit admission/order and caller-owned persistence.
   The two remote summary blobs are not an atomic pair; common source tokens
   expose their lineage and failed publication is replayed.
4. Existing cleanup UX now needs the explicit external all-writer shutdown/proof
   above, including ordinary wipes. No automatic timeout recovery, inferred
   quiescence or rolling mixed-writer compatibility is claimed.

## Validation

Latest verified combined backend suite: **5,154 passed, 14 skipped**. All 14 skips
require the unavailable private TIFF corpus. Frontend: **816 passed across
57 files**, rerun against the combined implementation. Coverage includes full/incremental equivalence,
source reservations/CAS, bounded fallback, failed publication/replay, noop/delta
summary drains, wave guards, conservative and periodic GC, fair bounded recovery
and quiesced purge retry/order. Patch whitespace checks are clean; unrelated
existing Bicep and frontend TypeScript configuration diagnostics remain untouched.
No deployment or infrastructure change was performed for this update; these
results do not certify a live rollout or production-scale sizing.

See [incremental-summaries.md](incremental-summaries.md) for the summary
adapters' separate reference contracts and operational limitations.