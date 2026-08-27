# ipworker background-processing architecture (as of 2026-08-27)

Scope: how server-side AI processing of uploaded photos actually works today —
dispatch, hardware, concurrency, scaling, coordination with the browser path,
and the per-step cost profile measured live this session. Everything below is
verified against the current code (`backend/app.py`, `backend/storage_utils.py`,
`backend/ipwork_*.py`, `deploy/resources.bicep`), not recalled from memory.

## 1. What ipworker is

A separate Azure Container App (`ownphotostore-ipworker` / `photostore-test-ipworker`)
running the same Flask codebase as `backend`/`worker`, but started via
`run_ipworker()` instead of gunicorn — a pure queue-consumer process, no HTTP
serving. It exists so photo processing (thumbnail, EXIF/GPS, OCR, face
detection+embedding, CLIP scene tagging, reverse geocoding) can happen
server-side instead of requiring a browser tab to stay open. Controlled by the
`PROCESSING_MODE` env var (`browser` / `backend` / `both`), which gates whether
the frontend's client-side AI pipeline runs at all (`AppServicesProvider.tsx`)
and whether the backend ever enqueues ipwork messages.

Six steps, defined once and shared with the browser path:
```python
IPWORK_STEPS = ('thumbnail', 'exif', 'ocr', 'face', 'ai_vision', 'map_detection')
```
(`backend/app.py:398`)

## 2. Hardware / deployment (`deploy/resources.bicep:664-791`)

- **Image**: its own image (`ipworkerImage`), not shared with `backend`/`worker`
  — it needs a much heavier dependency set (`requirements-ipworker.txt`: torch,
  open_clip_torch, onnxruntime, opencv-python-headless, mediapipe, pytesseract)
  that would otherwise bloat cold-starts for the plain backend/worker roles.
- **Per-replica resources**: `2.0 vCPU / 4Gi memory`. Sized to match `worker`'s
  footprint on the reasoning that "a single pass runs YOLO face detection,
  MediaPipe landmarks, AdaFace embedding, CLIP tagging, and tesseract OCR in
  sequence for one photo" — i.e. originally provisioned for **one photo at a
  time per replica**.
- **Scale**: `minReplicas: 0`, `maxReplicas: 4`. Scales to zero when idle.
- **2 vCPU/4Gi is a hard ceiling, not just today's choice.** This managed
  environment (`resource managedEnvironment`, `resources.bicep:185-188`) has
  `properties: {}` — no `workloadProfiles` configured, i.e. it's a plain
  Consumption environment. Azure caps Consumption-only environments at 2
  vCPU/4Gi per replica; going higher requires migrating the managed
  environment to Workload Profiles first, not just bumping a bicep number.
  Relevant if a future "just give it more CPU" plan comes up.

## 3. How work gets dispatched

A queue (`photostore-ipwork`), one message per (photo, set-of-steps) unit of
work, created by the backend at startup. `PROCESSING_MODE != 'browser'`
enqueues a message after each upload with whichever steps are outstanding.

**Two independent ways ipworker learns about work:**
1. **Direct enqueue** at upload time (or whenever the backend decides a step
   needs redoing, e.g. a face-embedding-version bump).
2. **Sweep** (`_ipwork_sweep_loop`, `backend/app.py:10195`) — a daemon thread
   inside every ipworker replica, independent of the queue-polling loop, that
   runs every `IPWORK_SWEEP_INTERVAL_SECONDS` (default 1200s / 20min) and
   re-enqueues any photo across *every* library whose steps look stuck: never
   queued at all (uploaded while ipworker was stopped, or during a
   `browser`-only window), `running` with an expired lease, `queued` for
   longer than `IPWORK_SWEEP_STALE_QUEUED_SECONDS` (1800s), or a `done` face
   whose embedding version is stale. This is the self-healing path for
   "photo never got enqueued in the first place."
3. Because minReplicas=0, a cron scale rule (`ipwork-sweep-wake`) wakes one
   replica for a 5-minute window every 20 minutes (`0,20,40 * * * *` →
   `5,25,45 * * * *`, UTC) purely so the sweep gets a chance to run even when
   the queue is empty — kept in sync with `IPWORK_SWEEP_INTERVAL_SECONDS`.

## 4. Concurrency model

Two independent levels of concurrency:

**Across replicas** — KEDA `azure-queue` scale rule on `photostore-ipwork`,
target `queueLength: 1` with `queueLengthStrategy: visibleonly` (counts only
genuinely unclaimed messages, not ones deliberately left undeleted for a
lease-race retry — see §5). 0 to 4 replicas.

**Within a replica** — `IPWORKER_CONCURRENCY` (currently `2`, `backend/app.py:441`,
default `1` if unset) sizes a `ThreadPoolExecutor` in `run_ipworker()`
(`backend/app.py:12970`). The main loop keeps exactly enough messages
in-flight to saturate that pool, refilling one slot at a time as futures
complete rather than batch-waiting (`backend/app.py:12942-12951`):
```python
free_slots = min(IPWORKER_CONCURRENCY - len(in_flight), 32)
```
**Total concurrent-photo ceiling = replicas × IPWORKER_CONCURRENCY = 4 × 2 = 8** today.

Per photo, within one worker thread, `_run_ipwork_steps` (`backend/app.py:12676`)
runs every requested step **sequentially** — one `for step in steps` loop, one
lazy image download shared across all steps, no intra-photo parallelism:
`thumbnail → exif → ocr → face → ai_vision → map_detection`. A slow step (OCR)
blocks everything after it for that photo; it does not block other photos,
since those run on their own thread-pool slots.

**Results are also batched, not streamed per-step — this matters more than
the ordering above.** `_handle_ipwork_queue_payload` calls `_run_ipwork_steps`
to completion (building one `client_processing` dict across all 6 steps) and
only *then* calls `apply_client_processing_results_for_file` once with the
whole dict. So today, nothing — not even the already-finished thumbnail —
becomes visible/persisted until OCR *and* every step after it also finishes.
Reordering `IPWORK_STEPS` alone would not fix this (the batched write still
waits for the slowest step regardless of order); the actual fix would be
either per-step persistence or splitting the fast steps into their own apply
call before OCR runs. Not implemented — a real UX-latency lever (time until a
thumbnail/face appears), distinct from raw photos/hour throughput, which this
change would not move.

`IPWORKER_CONCURRENCY`'s own comment (`backend/app.py:428-434`) documents the
last real benchmark that set it: Azure Monitor showed ~45% avg CPU per
replica at concurrency=1 (headroom), but memory was already the tighter
constraint at ~66-77% peak — i.e. the historical rationale for raising
concurrency was memory-bound, not CPU-bound, and CPU contention wasn't
re-checked when it was later pushed to 3. This session's timing data
(§7) is the first direct CPU-contention measurement against a live
concurrency value.

## 5. Coordination with the browser path (avoiding duplicate work)

In `both` mode, a browser tab and ipworker can both be trying to process the
same upload. Coordination is a single per-photo lease
(`claim_processing_lease`/`release_processing_lease`/`heartbeat_processing_lease`,
`backend/storage_utils.py:3013-3090`) stored directly on the metadata row:
`processing_lease_owner`, `processing_lease_expires_at`, plus a `{step}_status`
field per step (`pending`/`queued`/`running`/`done`/`no_data`/`failed`/`skipped`/
`unsupported`). Whoever claims the lease does the work; the loser backs off
without wasting inference. `IPWORKER_LEASE_SECONDS = 300` (generous vs. the
browser's 120s, because one ipwork pass does every step in sequence rather
than one at a time).

If ipworker loses the lease race (browser already claimed it) but the browser
tab then closes mid-processing, ipworker's queue message is deliberately left
**undeleted** so Azure redelivers it — retried up to `IPWORK_LEASE_RETRY_LIMIT`
(3) times, each retry costing one `IPWORKER_VISIBILITY_TIMEOUT_SECONDS` (300s)
wait. This is also why the KEDA scale rule needs `queueLengthStrategy:
visibleonly` — the default strategy would count these deliberately-undeleted
retry messages as real backlog and over-scale ipworker for work the browser
already finished.

`IPWORKER_VISIBILITY_TIMEOUT_SECONDS` also guards against a subtler bug: with
no visibility timeout, Azure defaults to 30s, and one ipwork pass can exceed
that — the same message would then get redelivered to a *second* replica
while the first is still working it, and because the redelivered copy carries
the same `jobId` (same lease-owner string), the ownership check wouldn't
block the second attempt. Two replicas could genuinely run the full model
pipeline on the same photo concurrently. Matching this to `IPWORKER_LEASE_SECONDS`
closes that gap.

## 6. Per-photo processing cost — real measured numbers (2026-08-27)

Added this session (`worker_logger.info` in `_run_ipwork_steps` and
`_handle_ipwork_queue_payload`, commit `e42de90`), captured live on
`photostore-test`:

**Per-step breakdown** (3 real photos):
| file | thumbnail | exif | **ocr** | face | ai_vision | map_detection |
|---|---|---|---|---|---|---|
| IMG_0078.JPG | 136ms | 1ms | **5404ms** | 1338ms | 840ms | 1ms |
| IMG_0369.HEIC | 1809ms | 7ms | **10387ms** | 1269ms | 2379ms | 116ms |
| IMG_0427.jpg | 340ms | 10ms | **6676ms** | 854ms | 1175ms | 87ms |

OCR (`ipwork_ocr.py` → `pytesseract.image_to_string`, a real subprocess exec
of the `tesseract` binary — CPU-bound, not async/network) dominates every
sample at 65-70% of that photo's step-sum, run **unconditionally** regardless
of whether the photo has any text. `map_detection` (Nominatim reverse-geocode
HTTP call, 8s timeout) is negligible (1-116ms) — ruled out as a suspect this
session.

**Per-message breakdown** (everything `_run_ipwork_steps` doesn't cover:
lease claim, `apply_client_processing_results_for_file`, people-clustering
enqueue):

| Concurrency | steps_ms | lease_claim_ms | apply_ms | cluster_ms | total_ms |
|---|---|---|---|---|---|
| =3 (pre-fix) | 20205-77127 | ~20-60 | ~200-800 | 0-1900 | 20205-77484 |
| =2 (post-fix) | 10536-13319 | ~23-33 | ~205-215 | 0 | 10786-13584 |

`lease_claim_ms`/`apply_ms`/`cluster_ms` are consistently negligible (tens to
low hundreds of ms) — **steps_ms is >90% of total_ms in every sample**, so all
the actionable cost lives inside the sequential per-step loop, not the
surrounding bookkeeping.

**Aggregate throughput**, measured via `thumbnail_status='done'` row growth on
the `photometadata` table over clean 2-minute windows:
- `IPWORKER_CONCURRENCY=3` on 2 vCPU: **~440 photos/hour** (all 4 replicas,
  8 replica-level slots but oversubscribed at 12 concurrent OCR/face threads
  contending for 8 real cores).
- `IPWORKER_CONCURRENCY=2` on 2 vCPU: **~2052 photos/hour** (4.7x). Matches
  concurrency slots × 3600s ÷ ~14s-per-slot ≈ 2057/hr — i.e. throughput is now
  well-explained by the measured per-photo latency, not still bottlenecked by
  contention.
- `IPWORKER_CONCURRENCY=2` + `OMP_THREAD_LIMIT=1` on 2 vCPU: **~2724 photos/hour**
  (+33% more). See below — a second, previously invisible layer of the same
  oversubscription bug.

**Root cause of the pre-fix gap**: `IPWORKER_CONCURRENCY=3` was raised
out-of-band (found as bicep/live drift 2026-08-25) without re-checking CPU
headroom against the 2 vCPU allocation. OCR (subprocess) and face detection
(YOLO+MediaPipe+AdaFace, `onnxruntime`) are both genuinely CPU-bound; 3
concurrent threads doing CPU-bound work on 2 real cores means each one
individually gets slower the more of its neighbors are also mid-OCR/face —
textbook oversubscription. Confirmed by per-message `total_ms` running 2-5x
higher than any single photo's own step-sum measured when contention was
lower.

**Second layer of the same bug, found 2026-08-27**: nothing in
`ipworker.Dockerfile`/`entrypoint.sh` ever set `OMP_THREAD_LIMIT` (or any
other tesseract thread cap) — confirmed by reading both files. This image's
tesseract build spawns multiple OS threads *per `image_to_string()` call* by
default, so even at the already-fixed `IPWORKER_CONCURRENCY=2`, two
concurrent OCR calls were each internally multi-threaded and still fighting
over 2 real cores. Setting `OMP_THREAD_LIMIT=1` (live env var, then committed
to `resources.bicep`) measured a one-sample OCR drop from 5474ms to 2439ms
and the aggregate throughput gain above — no correctness impact, since this
only bounds internal parallelism, not OCR output.

## 7. OCR cost-reduction experiments, all rejected on real data so far

- **MSER text-region pre-check** (skip OCR if no text-like blobs found):
  scored 50 real photos with known ground truth — score distributions
  overlapped heavily (mean 72.9 has-text vs. 58.4 no-text; some texture-heavy
  no-text photos scored *higher*, up to 228, than any real text photo, max
  161). Not separable; would only safely filter ~24% of no-text photos.
- **Downscale image before OCR** (cut tesseract's own per-call cost): full
  resolution found text in 8/8 known-text photos; downscaled to 1000px found
  it in only 1/8. Too aggressive — most of this library's real text (signs,
  plaques) is small/medium relative to full frame.
- **Already-computed CLIP `ai_vision` tags** (vocabulary includes
  `document`/`screenshot`/`sign`/`map`) as a free existing signal: the one
  real example checked had CLIP tags come back empty/low-confidence for a
  photo whose real OCR text was a legible sign — not reliable either.
- **Real, controlled comparison**: browser-side OCR (`tesseract.js`, WASM) vs.
  backend OCR (`pytesseract`, native binary) on the *same 8 photos, same
  machine* — browser was **3-8x slower**, not faster, and also noisier
  (found spurious "text" on all 4 known-no-text control photos). Backend OCR
  is not the underperforming side of this comparison.

### EAST text-detector + crop OCR — rejected (2026-08-27)

**Status: NO-GO.** Proposal: run OpenCV's EAST text detector
(`cv2.dnn`, frozen graph, no new pip dependency since
`opencv-python-headless` is already pinned) on a resized copy of the image,
map detected boxes back to original-resolution coordinates, crop from the
*original* pixels (not the resized copy, to avoid repeating the downscale
failure above), and run the existing `pytesseract` only on those crops
instead of the full frame. Prototype: `backend/ipwork_ocr_crop_experiment.py`
(standalone, not wired into `IPWORK_STEP_PROCESSORS` — experimental only).
Tested detector input sizes 320/512/640.

The first benchmark pass (50-photo sample keyed off `ocr_status`) looked like
a strong win — 6-9x faster — but that ground truth turned out to be
worthless: inspecting the actual strings showed 24 of 25 `ocr_status='done'`
photos were tesseract hallucinating text-like noise from texture/grain, not
real text. Rebuilt the corpus by **visually inspecting all 50 candidate
photos directly** (not trusting `ocr_status` at all) and hand-verifying 8
genuine positives spanning large/clear, medium, small, and difficult
(handwriting, angled+glare, low-contrast screen) cases, plus 12 photos
visually confirmed to have zero real text.

| Metric | Baseline | EAST 320 | EAST 512 | EAST 640 |
|---|---:|---:|---:|---:|
| Meaningful recall (≥1/3 tokens), 8 positives | 12% | 12% | 12% | 12% |
| Any-overlap recall | — | 12% | 12% | 38% |
| False positives, 12 verified-clean negatives | 58% | 0% | 0% | 8% |
| Mean latency, positives | 1103ms | 322ms (3.4x) | 402ms (2.7x) | 538ms (2.1x) |

Go/no-go bar was ≥90% recall retention at ≥2x speedup. Best case (640px) hit
38% any-overlap recall — fails by a wide margin, not a marginal miss.
**EAST+crop must not replace full-image OCR in production.**

### Data-quality finding, independent of the EAST result

Building the verified corpus above surfaced something orthogonal to EAST:
**`ocr_status='done'` does not mean "this photo has useful text" — only that
tesseract returned *some* string.** On the same 8 hand-verified real-text
photos, baseline itself only hit **12.5% meaningful recall** (one easy case);
on the PCB label, both jacket-logo photos, and the TV/whiteboard photo it
reported `hasData=True` but returned pure noise unrelated to the actual
printed text — zero token overlap despite "succeeding."
Baseline also missed 3 of the 8 verified positives *entirely*, including a
large handwritten note filling the frame and a car's license plate — both
unambiguous to a human. Combined with the 58% false-positive rate on clean
negatives above, `ocrText`/`ocr_status` is a weaker quality signal than its
name implies for anything downstream that trusts it (e.g. text search over
photos, if that exists) — worth tracking as its own concern, separate from
this throughput investigation.

### Tesseract configuration sweep — evaluated, no production change (2026-08-27)

**Status: evaluated / no-go for a standalone production change** (distinct
from the rejections above — this isn't a failed technique, it's a real
precision/recall trade-off with no config on the frontier that clears the
bar alone). `ipwork_ocr.py`'s docstring claims it "mirrors" the browser's
`tesseract.js` call, but the browser (`PhotoGallery.tsx:1525-1546`) explicitly
sets `tessedit_pageseg_mode: '11'` (PSM 11, sparse text — no layout
assumption) while the backend passes no config at all, defaulting to PSM 3
(assumes a structured page/column layout — wrong for scattered scene text).
Swept PSM 11, PSM 11 + OEM 1, and PSM 11 + the browser's own grayscale/contrast
fallback preprocessing (`PhotoGallery.tsx:1479-1515`, normally only applied on
a retry) against the same 20-photo verified corpus.

| Metric | Baseline PSM 3 | PSM 11 | PSM 11 + OEM 1 | PSM 11 + gray/contrast |
|---|---:|---:|---:|---:|
| Meaningful recall (≥1/3 tokens), 8 positives | 12% | 12% | 12% | **38%** |
| Any-overlap recall | 12% | **62%** | 62% | 62% |
| False positives, 12 verified-clean negatives | 58% | **100%** | 100% | 100% |
| Mean latency, positives / negatives | 1098/2160ms | 1152/2949ms | 1143/2935ms | 1287/2564ms |

PSM 11 recovers real text PSM 3 misses entirely (handwritten note, PCB label,
partial TV/whiteboard text) — but it also flags **all 12** verified-clean
negatives as `hasData=True`. Sparse-text mode's willingness to call texture
"text" is exactly what buys the extra recall; it isn't a bug, it's the same
knob in both directions. Grayscale/contrast on top improves meaningful recall
further (12%→38%) but inherits the identical 100% false-positive rate, plus
~17% more latency. OEM 1 changes nothing material.

**Decision:** no configuration tested improves quality without also
collapsing precision. Not changing `ipwork_ocr.py` off PSM 3 on this
evidence. This also independently explains the earlier finding that browser
`tesseract.js` (PSM 11 already) was noisier than backend PSM 3 — same
trade-off, same root cause, corroborated on the native binary now too.

**Reinforces the `ocr_status` finding above, doesn't fix it**: naively
raising Tesseract's sensitivity would take `ocr_status='done'` from "58%
meaningless" to "100% meaningless" on true-negative photos. A precision-side
fix (something that reliably says "no text here, don't bother") would need
to exist before a higher-recall Tesseract config could be exposed to
anything that trusts `ocrText`/`ocr_status` — and neither MSER nor EAST
(above) turned out to be that.

### CLIP embedding as a precision-oriented OCR-skip gate — rejected after scaling up (2026-08-27)

**Status: rejected.** The config sweep above established that the real
blocker isn't OCR sensitivity, it's precision — every attempt to catch more
real text also caught more noise. This asked a narrower question than MSER
did: not "does this image contain text" in general, but specifically, can a
**conservative skip decision** (only skip OCR when confidently safe) be
built from a signal the pipeline already computes? `ipwork_vision.py`'s CLIP
step already produces a full image embedding every photo
(`vision_utils.encode_image_embedding`) before running softmax over the full
tag vocabulary — the vocabulary approach was tried and rejected earlier
(dilutes any one tag's score against dozens of unrelated categories), but
the raw embedding itself was never tested directly.

Reused the exact production embedding function, computed raw cosine
similarity against 7 hand-written "contains text" prompts and 5 "no text"
prompts (`vision_utils.encode_text_embeddings_batch`, same checkpoint, zero
new model/inference cost if reordered ahead of OCR), and looked at the
margin (no-text similarity − text similarity).

**First pass (n=8 positive / 12 negative, the existing verified corpus)**
looked like a real, correctly-shaped signal: a threshold at margin > 0.006
kept all 8 positives below it while letting 8/12 (67%) of negatives safely
skip OCR — unlike MSER's heavily overlapping distributions. Flagged at the
time as too small a sample to trust (0.001 gap between the riskiest positive
and nearest negative) and not implemented.

**Second pass, at real scale:** two new local sources were supplied to
properly validate this — the RVL-CDIP document dataset (a public scanned-
document classification corpus, ~3500 images, all real text, no negatives)
and a local export of `photostore-test`'s own source photo library (~5900
real photos, same population the ipworker corpus was originally sampled
from). Visually inspected 70 photos sampled from that export (same manual-
verification method as the original 20-photo corpus) and found 18 more
genuine real-text positives plus 50 confirmed-clean negatives — a 4-6x
larger, still fully domain-matched corpus — and separately sampled 40
RVL-CDIP images as an easy-case sanity check.

| Group | n | margin min | margin max | margin mean |
|---|---:|---:|---:|---:|
| Export positives (real photos, has text) | 18 | -0.062 | **+0.026** | -0.015 |
| Export negatives (real photos, no text) | 50 | **-0.031** | +0.045 | +0.010 |
| RVL-CDIP positives (scanned documents) | 40 | -0.099 | -0.038 | -0.073 |

**The separation collapsed.** At the only threshold that guarantees zero
false skips (0.026, the new highest positive margin), just **8 of 50
negatives (16%)** are safely skippable — down from the small sample's 67%.
The positive/negative margin distributions genuinely overlap (171 of 900
possible pairs cross over). The n=8 result was exactly the small-sample
artifact it was flagged as a risk of being. Notable side-finding: RVL-CDIP
documents score far more confidently "has text" (mean -0.073) than personal
photos with embedded text (mean -0.015) — CLIP handles "whole frame is a
document" easily; it's specifically small/embedded text in an otherwise
ordinary photo (jacket logos, product labels, distant signs) it can't
reliably separate from no-text photos via this margin approach.

**Not pursuing this gate.** Same fate, same root cause as MSER: plausible
on a small sample, doesn't hold at real scale — this time confirmed with a
proper validation pass instead of assumed.

### Relationship between the approaches tried

- MSER: unreliable binary text/no-text signal (scores don't separate).
- Whole-image downscale: real text lost, not a false-positive problem.
- Browser tesseract.js: correctness aside, just slower (3-8x) than backend.
- EAST+crop: localization quality insufficient despite genuine 2-3x speedup.
- Tesseract PSM/OEM/preprocessing sweep: recall/precision trade-off, no
  config clears the bar alone.
- CLIP-embedding skip gate: looked real at n=8, collapsed at n=68 — same
  small-sample trap as MSER.

**All cheap/existing-signal paths are now exhausted.** The remaining option
is a learned end-to-end OCR system with jointly-trained detection+recognition
(e.g. PP-OCR) — a separate, multi-day project, not a continuation of
anything above. The verified corpus built across these experiments (68 real
photos + 40 documents, all in this session's scratchpad, not yet committed
to the repo) is reusable ground truth for whichever OCR change comes next.

## 8. Adjacent per-photo-hot-path bugs already fixed (context for "what else could still be lurking")

Same bug class each time — O(library-size) work re-triggered by a
single-photo event, with no debounce:
- **Vector index**: `_apply_client_processing_results` used to call
  `refresh_user_vector_index` (full-partition scan + re-embed + blob
  re-upload, ~20s on a 6k-photo library) after *every* photo. Fixed to
  `touch_user_vector_index_state` (cheap dirty-flag write); the expensive
  rebuild now only runs lazily when someone actually does a semantic search.
- **People/face cache**: `PEOPLE_SCAN_CACHE_TTL_SECONDS` defaults to 20s
  (tuned for interactive backend requests), but one ipwork pass historically
  took 60-90s — every photo re-scanned the full people/face partition because
  the cache had already expired by the next photo. ipworker overrides this to
  120s.
- **Clustering worker**: used to fully re-run DBSCAN over all faces on every
  new-face event; fixed to incremental.

Both of ipworker's own timing/config comments (`app.py:428`, `resources.bicep:687`)
explicitly flag "re-benchmark before changing this again" — this file exists
so that re-benchmarking has somewhere to start from instead of re-deriving
from scratch.

## 9. Summary of current numbers, for reference while analyzing

| Metric | Value |
|---|---|
| Replicas (min / max) | 0 / 4 |
| vCPU / memory per replica | 2.0 / 4Gi |
| Concurrency per replica | 2 |
| Total concurrent-photo ceiling | 8 |
| Dominant per-photo cost | OCR, 2.4-10.4s (65-70% of step time) |
| Per-photo wall time (current) | ~10.8-13.6s (pre-OMP-fix figure; not yet re-measured per-step post-fix) |
| Measured aggregate throughput | ~2724 photos/hour (all 4 replicas, with OMP_THREAD_LIMIT=1) |
| Per-slot throughput | ~341 photos/hour |
| Tesseract thread cap | `OMP_THREAD_LIMIT=1` (added 2026-08-27) |
| Scale-out trigger | queue length ≥1 (visible-only), or cron wake every 20min |
| Lease TTL / queue visibility timeout | 300s / 300s |
| Lease retry limit | 3 attempts |
| Sweep interval | 1200s (20min) |
