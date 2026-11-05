# Vision throughput gap analysis: current state and the path to 10,000/hour — 2026-10-29

## Scope and method

Read-only Log Analytics investigation of production wave `f54259517fe84c19864ec308b0682ad8`
(the ~9.7k-file upload that finished around 11:14 CET / 10:23 UTC on 2026-10-29),
cross-referenced against the deployed configuration (`deploy/resources.bicep`),
the wave-execution/ownership code (`backend/execution_lease.py`,
`backend/upload_wave_execution.py`, `backend/wave_runtime.py`), and the
per-image apply-path code that shares the `photojobs` table
(`backend/processing_receipts.py`, `backend/upload_waves.py`,
`backend/index_work.py`). One local synthetic benchmark was run
(`backend/scripts/benchmark_wave_source_batch.py`) to pre-check the
coordination layer at higher concurrency. **No Azure configuration, deployment,
or production data was changed.** No real-model Azure benchmark was run —
that step still requires an explicit go/no-go (cost + resource creation) before
execution, consistent with how the existing `vision-image-batching-2026-10-27.md`
and `wave-vision-batching-2026-10-26.md` audits gated their own Azure jobs.

All findings below are either (a) directly measured from Log Analytics /
`vision_wave_attempt_summary` / `vision_image_summary` telemetry for this one
wave, (b) read directly from committed source and infra-as-code with file:line
citations, or (c) a local synthetic benchmark result. Nothing here is a
production throughput claim beyond what this single wave's telemetry shows;
treat it as one data point, not a controlled A/B.

## Part 1 — Measured throughput for wave `f54259...`

Source: `ipwork throughput metrics=` records (per-replica tumbling-window
counters) and `vision_image_summary`/`vision_wave_attempt_summary` events,
both emitted by the deployed `forenklaprep1-dev-vision` container, queried
from the `forenklaprep1-dev-law` Log Analytics workspace for
2026-10-29T08:00–10:35Z.

### 1.1 Fleet-level throughput

| Metric | Value |
|---|---|
| Images with preview reused (`window.preview_reused`, the reliable per-image proxy — see 1.2) | 9,678 (sum across all replicas/windows) |
| Replica wall-time covered | 8,453s (≈2h21m), three container-group instances, strictly sequential (no concurrent overlap) |
| Sustained rate (total images / total wall-time) | **4,122 images/hour** |
| Mean of per-minute sample rates | 4,068/hour |
| Peak per-minute sample rate | **5,676/hour** |
| Mean CPU used | 0.91 of 2 allocated vCPU (≈45%) |
| Mean pipeline slot occupancy (`utilization.window.slot_utilization`) | 44% of configured concurrency=2 |

Three container-group instances served this window, in strict handoff (no
overlap): `...-0000034-...-b9ftn` (08:06–08:34 UTC), then
`...-0000035-...-r6vps` (08:38–09:17 UTC), then `...-0000035-...-k8pr5`
(09:19–10:27 UTC). The two gaps (08:34→08:38, 09:17→09:19) correspond to a
revision rollout and a replica handoff respectively, both representing lost
wall-time with zero throughput.

### 1.2 Why `preview_reused` was used instead of the counters the collector calls "productive"

The documented productive-work counters (`done`, `productive_completed`) were
**not usable** for this measurement: summed across the whole window they were
`done=81`, `productive_completed=0`. This is the known instrumentation gap
flagged in `docs/audits/processing-efficiency-2026-10-26.md`: *"Existing legacy
`productive_completed` stays zero even while wave-source commits succeed"* —
the wave-batched consumer path (post `c0cb6d0`) never updates these per-queue-
message counters designed for the old one-message-per-image model.
`window.preview_reused` (9,678, matching the ~9.7k uploaded files) was used
instead because every image in this pipeline passes through the shared
preview-reuse path exactly once (`backend/app.py:15502`'s `get_image_bytes`),
making it a reliable per-image proxy — but it is still a proxy, not a counter
designed for this purpose. **Recommendation for anyone re-running this
measurement:** add a counter that increments once per genuinely new completed
source under the wave-batched path; today there is no purpose-built field for
this and every measurement (including this one) has to improvise a proxy.

### 1.3 Success / failure accounting (authoritative, not a proxy)

Unlike throughput, success/failure has a purpose-built, authoritative source:
the final `vision_wave_attempt_summary` event for this wave_id
(`measurement_scope: "attempt"`, emitted once per attempt/page-group), which
carries `member_count`, `failed_members`, and `vision_complete`.

Progression of `failed_members` across the 71 attempt-summary samples for this
wave, in time order:

- 08:12:45–10:13:18 UTC (62 samples): `failed_members` absent (not yet
  evaluated), `member_count=9731` throughout.
- 10:15:04–10:21:45 UTC (5 samples): `failed_members=3`.
- **10:23:13 UTC (final sample): `vision_complete: true`, `failed_members: 3`,
  `member_count: 9731`.**

| Metric | Value |
|---|---|
| Total members (files) in wave | 9,731 |
| Permanently failed (exhausted retries, settled `failed_exhausted`) | 3 |
| Successfully processed | 9,728 |
| **Success rate** | **99.97%** |

Separately, at the per-image level (`vision_image_summary`, `outcome` field),
**5 distinct files** briefly logged `outcome:"error"` at exactly 08:34:16
UTC — the same timestamp as the revision rollout
(`...-0000034`→`...-0000035`). All 5 were confirmed to have a later
`outcome:"success"` event for the same `file_id` and are **not** among the 3
permanently-failed members; they were transient failures during the replica
handoff, successfully retried. The 3 permanent failures are a distinct,
unexplained set — no corresponding error-outcome event was found for them in
this window, meaning whatever caused them to exhaust retries happened through
a path that doesn't emit `vision_image_summary` (plausible given the bounded
skip-and-settle logic added in `32b9e9b`, which settles stuck members without
necessarily running another model attempt). **This is the one open thread
from this audit**: the 3 failures were not root-caused, only confirmed to be
real (not an artifact of the transient-error retries).

### 1.4 Top time-consuming actions (per-image, summed across all images in the wave)

Source: every numeric `*_ms` field in `vision_image_summary` events for this
wave, summed across 6,374 distinct `file_id`s / ~5,500 nonzero-phase images
(the remainder were cover-only or skipped passes).

| Rank | Action/Phase | Total (all images) | Avg/image | Note |
|---|---|---|---|---|
| 1 | `vision_source_busy_ms` / `image_processing_ms` | 1,669.0 hrs | 1,093 ms | Top-level wrapper for the whole per-image pipeline; these two fields are the same measurement under two names. |
| 2 | `vision_source_steps_ms` | 794.0 hrs | 520 ms | Model-inference stage; child of #1. |
| 3 | `vision_source_apply_ms` | 782.1 hrs | 512 ms | Persist/commit stage; child of #1. Roughly equal to #2 — apply is **not** cheaper than inference. |
| 4 | `storage_send_ms` | 697.3 hrs | 456 ms | Cross-cutting SDK network-send accounting; overlaps with the I/O rows below. Not independently additive on top of them. |
| 5 | `face_embed_ms` (AdaFace) | 310.9 hrs | 208 ms | Largest single model-compute leaf, inside #2. |
| 6 | `clip_inference_ms` | 205.2 hrs | 134 ms | Inside #2. |
| 7 | `semantic_text_request_ms` | 174.2 hrs | 114 ms | CLIP text encode for semantic search; inside #2. |
| 8 | `io_photojobs_read_ms` | 160.2 hrs | 105 ms | Job-table reads; inside #3. See Part 3 — traced to three independent subsystems sharing one table. |
| 9 | `text_inference_ms` | 158.3 hrs | 104 ms | Subset of #7. |
| 10 | `face_detect_ms` (YOLO) | 148.8 hrs | 97 ms | Inside #2. |

Just outside the top 10: `io_photometadata_read_ms` (142.0 hrs, 93ms/img) and
`io_photofacebyfilename_read_ms` (68.4 hrs, 45ms/img) — the largest storage-read
contributors inside apply besides `photojobs`.

**Headline split:** per image, roughly **half the busy time is model compute
(520ms), half is Azure Table/Blob I/O during apply (512ms)**. Nothing in the
top 10 is a single runaway cost — it's a flat, wide distribution across ~10
phases each contributing 100-500ms, which is itself informative: there is no
single fix that collapses this; throughput gains have to come from either
running more of these per-image pipelines concurrently, or trimming several
mid-sized phases at once.

## Part 2 — Why the system is concurrency-bound, not CPU- or model-bound

### 2.1 The arithmetic

At the deployed `IPWORKER_CONCURRENCY=2`
(`deploy/resources.bicep:1702`), the theoretical ceiling for one replica is:

```
2 concurrent slots × 3600s / 1.093s busy-time-per-image ≈ 6,588 images/hour
```

The measured peak (5,676/hour) sits at 86% of that ceiling; the measured
sustained rate (4,122/hour) is lower still, pulled down by the two
zero-throughput replica-handoff gaps in section 1.1 and by `slot_utilization`
averaging only 44% (i.e. even within concurrency=2, both slots were busy less
than half the time — page-boundary serialization, metadata-batch fetches, and
per-page commits all create brief all-slots-idle gaps between pages of 20).
Mean CPU usage was 0.91 of 2 allocated vCPU — **less than one full core busy
on average**, while two full cores are provisioned. The system is not CPU-
saturated; it is concurrency-limited by a setting that caps work at 2
simultaneous images per replica.

### 2.2 Why concurrency is 2, and why that reason may no longer hold

`deploy/resources.bicep:1690-1716` documents an exact history:

- `IPWORKER_CONCURRENCY` was informally raised to 3 at some point (discovered
  as infra-drift on 2026-08-25).
- It was lowered back to 2 on 2026-08-27 after per-step timing logs showed 3
  concurrent OCR/face passes — both CPU-bound (`pytesseract` shells out to the
  `tesseract` binary; face runs YOLO+AdaFace) — fighting over only 2 vCPU.
  Per-message wall time ran 2-5x higher than any single photo's own step-sum:
  the signature of CPU oversubscription, not I/O wait.
- The same investigation found a **second, previously invisible** layer of
  the identical bug: the deployed `tesseract` build spawns multiple OS threads
  per `image_to_string()` call by default. At concurrency=2 on 2 vCPU, two
  internally-multithreaded OCR calls were still oversubscribing two real
  cores. Capping it (`OMP_THREAD_LIMIT=1`) measured a one-sample OCR drop from
  5,474ms→2,439ms and a live aggregate throughput gain of 2,052/hr→2,724/hr
  (+33%).
- `OPENBLAS_NUM_THREADS=1` (comment at `:1717-1721`) was set for the same
  reason on the NumPy/OpenCV/OpenBLAS side: a matched two-CPU run measured
  29.5% less stage wall time at one BLAS thread with identical outputs.

**The critical sequencing issue:** concurrency was reverted to 2 *before*
`OMP_THREAD_LIMIT=1`/`OPENBLAS_NUM_THREADS=1` were confirmed to fix the
underlying oversubscription, and concurrency was never re-raised afterward to
check whether the fix was sufficient on its own. The decision to stay at 2 is
now almost 2 months stale relative to the fix that targeted its stated cause.
Separately, this 2026-08-27 investigation also predates the entire October
apply-path optimization effort (artifact consolidation, face-write batching,
preview reuse, read/buffer/prefetch — commits `71227ca` through `32b9e9b`),
which measurably shifted the per-image time mix: apply (I/O-bound, GIL-
releasing network waits) is now roughly as expensive as model inference
(512ms vs 520ms per image, section 1.4), where in August it was presumably a
smaller fraction. More of today's "busy" time is spent waiting on network,
not computing — exactly the kind of time that *more* concurrent threads can
overlap without proportionally increasing CPU contention, unlike August's
all-CPU-bound OCR/face mix.

### 2.3 Local synthetic pre-check (coordination layer only)

Before considering any real change, `backend/scripts/benchmark_wave_source_batch.py`
was run locally (via the repo's existing `.venv-photostore-tools` venv) at
increasing `source_workers`, using synthetic `time.sleep()`-based model/storage
costs. This exercises the *real* wave/receipt/checkpoint coordination logic
with zero real CPU or model contention — it answers "does the coordination
layer itself become a bottleneck at higher concurrency," not "is CPU
oversubscribed."

| `source_workers` | `after.seconds` (100 synthetic photos, 50ms model / 2ms storage) | Speedup vs pre-wave-batching baseline (`7e76cff`) |
|---|---:|---:|
| 2 | 12.66 (200 photos run) | 4.88x |
| 3 | 5.06 | 4.09x |
| 4 | 4.30 | 4.80x |
| 6 | 3.78 | 5.10x |
| 8 | 3.47 | 5.76x |

No new serialization point appears up to 8 workers (the page size,
`MAX_PAGE=20` in `backend/upload_waves.py:22`, bounds `source_workers` to
at most 20). **This derisks the coordination side of raising concurrency, but
proves nothing about CPU** — `time.sleep()` does not hold the GIL or burn
cycles the way real `tesseract`/YOLO/AdaFace/OpenBLAS calls do. The real
question (does raising `IPWORKER_CONCURRENCY` to 3-4 reproduce the August
oversubscription now that the thread caps exist) can only be answered by a
real-model Azure benchmark, structured like `vision-image-batching-2026-10-27.md`'s
pinned-digest temporary job. **That benchmark was not run during this audit** —
it requires spinning up a billable Azure Container Apps job, which needs an
explicit go-ahead before execution.

### 2.4 Why horizontal scaling (replicas) is a dead end for a single large wave — deferred per direction, documented for completeness

Two independent mechanisms cap a single wave to exactly one replica,
regardless of `maxReplicas`:

1. **The KEDA autoscale rule's metric no longer reflects image volume.**
   `wave_runtime.send_stage()` enqueues **one queue message per wave**,
   lease-renewed for the wave's entire duration
   (`deploy/resources.bicep:1802-1806`: *"A wave message can own hundreds of
   images... count it until ACK"*). The `queueLength: 2` rule
   (`:1790-1801`, tuned 2026-09-21 back when one message ≈ one image) sees at
   most 1 message in flight for any single wave no matter its size — a
   9,731-member wave and a 10-member wave look identical to this metric.
2. **Stage ownership is exclusive by design, independent of the queue.**
   `run_vision_release()` in `upload_wave_execution.py:530` calls
   `claim_release(leases, store, library, wave_id, 'vision', ...)` — a single
   stage lease per `(library, wave_id)`, documented in
   `execution_lease.py:11` as covering *"one wave's whole stage, not one
   photo."* Whichever replica claims a wave owns its entire member-page loop;
   there is no mechanism to split one wave's pages across replicas.

Per your direction, this is **deferred** — the target is single-replica
throughput — but it's documented here because it means `maxReplicas` and the
KEDA rule are **not currently part of the single-wave throughput ceiling at
all**; raising `maxReplicas` without also changing (1) and (2) would have zero
effect on a single large burst like this one. They would only matter for
*concurrent, independent* waves (e.g. two users uploading simultaneously).

## Part 3 — The `photojobs` read cost, traced to source

`io_photojobs_read_ms` (105ms/image, #8 in the Part 1.4 ranking, 160.2 hours
aggregate) comes from 11-15 separate GETs against the Azure Table named by
`JOBS_TABLE` (`backend/app.py:537`, default `photojobs`) per image. This
counter is generic SDK-level HTTP tracing (`processing_metrics.record_io`,
`backend/processing_metrics.py:139-161`): it classifies every table-storage
REST call by the resource name in its URL, and `photojobs` GETs are tagged
`job_read_requests`/`io_photojobs_read_*` regardless of which application
subsystem issued the call. **It is not a counter any one module owns** — which
turns out to be exactly why it was overlooked.

Tracing `jobs_table_client` usage (`backend/app.py`, ~60 call sites) found
**three independently-designed subsystems**, each unaware of the others,
hitting the same physical table per image during vision's apply path:

1. **`ArtifactReceipts`** (`backend/processing_receipts.py:21-85`). `capture()`
   publishes the immutable thumbnail/preview/face receipt via a create-only
   `get_entity`/`submit_transaction` against partition
   `'processing-artifacts-' + digest(library)`. This is the subsystem the Oct
   27 consolidation (`docs/audits/vision-apply-consolidation-2026-10-27.md`)
   actually touched — it cut *this subsystem's own* redundant rereads (5
   metadata GETs → 1 in the measured case). It did not look outside
   `ArtifactReceipts` itself.
2. **`ShadowWaves.accept_source_completion`** (`backend/upload_waves.py:159-206`).
   A separate idempotency mechanism: `existing()` issues its own `_get(key)`
   GET against a `'source-completion!' + digest([photoId, revision]) + '!' +
   intentId` key in the **same table**, then attempts a create-only
   transaction, then on a 409 conflict issues **another** GET via `existing()`
   again. This is the completion-inbox accept path — logically unrelated to
   artifact receipts; it only shares infrastructure with (1) because both
   happen to be backed by `jobs_table_client`.
3. **`index_work.WorkStore`** (`backend/index_work.py:111-125`). The
   outbox/dispatcher used for completion delivery and the
   `IPWORKER_INDEX_REBUILD_MILESTONE` index-rebuild trigger
   (`backend/app.py:1759-1766`), also backed by `jobs_table_client`, with its
   own `_get` calls.

Each subsystem was built at a different time for a different purpose
(artifact immutability, completion-acceptance idempotency, outbox dispatch),
and each treats the table as its own. None individually looks expensive (1-2
GETs each) — the cost only becomes visible once `io_photojobs_read_*` is
summed across the whole apply path and totals 11-15 calls/image. This is
precisely the failure mode the user's framing predicted: optimization effort
chased the three loudest named phases (steps/apply/storage_send) and the
subsystem that happened to be *inside* one of them and already had a name
(`ArtifactReceipts`), while two siblings sharing the same table by coincidence
of infrastructure, not design, went unexamined.

**Concrete, low-risk next step (code-only, no deploy/cost required):**
`ArtifactReceipts.capture()` and `ShadowWaves.accept_source_completion()` run
back-to-back in the same apply call for the same image/library (both are
invoked from the vision completion/apply path per source). Their respective
existing-key lookups could plausibly share one batched read the same way the
Oct 27 pass coalesced promotion+capture reads — **this was not yet traced to
confirm they are adjacent in the same request path**; that trace is the
immediate follow-up, not yet done as of this document.

## Part 4 — Recommendations, by status

| # | Recommendation | Status | Blocking step before change |
|---|---|---|---|
| 1 | Raise `IPWORKER_CONCURRENCY` to 3-4, re-test with `OMP_THREAD_LIMIT`/`OPENBLAS_NUM_THREADS` caps already in place | **Approved to pursue** | Real-model Azure benchmark (pinned-digest temp job, same pattern as `vision-image-batching-2026-10-27.md`) — not yet run; needs explicit go-ahead (billable, creates a cloud resource) |
| 2 | Fix KEDA scale metric to reflect actual wave size / enable multi-replica splitting of one wave | **Deferred** — out of scope while targeting single-replica throughput | N/A while deferred |
| 3 | (merged into #1) | — | — |
| 4 | Consolidate `ArtifactReceipts` + `ShadowWaves.accept_source_completion` reads against `photojobs` | Traced to source in Part 3; exact call-adjacency not yet confirmed | Read `upload_wave_execution.py`/`processing_completion.py` call sequence to confirm both run in the same per-image apply call before proposing the specific consolidation |

## Caveats

- This is one wave's telemetry, not a controlled multi-run benchmark. Revision
  rollout mid-run (section 1.1) means part of the measured window reflects two
  different deployed images back to back.
- The 3 permanently-failed members (section 1.3) were not root-caused — only
  confirmed distinct from the 5 transient retry errors.
- No code or configuration was changed by this audit. The synthetic benchmark
  (section 2.3) validates coordination scaling only, not CPU/model behavior
  under real concurrency — do not treat the 4-6x "speedup" figures there as a
  production throughput prediction; they measure a different thing (ETag/
  transaction coordination overhead with zero real compute cost).
