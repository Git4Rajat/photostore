# Upload processing audit — 2026-10-11

**Update after expanded negative testing:** recovery is **disabled by default**.
It added seven false-positive animal-head cases in a 580-case negative suite.
The recall figures below describe local experimental results, not a deployable
fix. See [the negative validation report](face-negative-validation-20261011.md).

The 1,130-photo upload completed successfully. The 56 worker processing flags
comprise 51 persisted face failures and five flags without a persisted step
failure. They do not represent 56 failed uploads.

The user confirmed that **all 1,130 source photos contain faces**. With that
ground truth, the 63 detector-zero results are missed detections. In total,
**69 photos (6.1%) saved no faces**, despite containing faces: 63 detector misses,
four landmark failures and two quality-gate rejections. At least one face was
saved for 1,061 photos (93.9%). This is photo-level face coverage, not individual
face recall; some photos may contain multiple faces that were not all recovered.
The 56 worker flags therefore undercount the face coverage problem.

## Scope and evidence

- Deployment: `forenkladev-rg`, upload revision `0000017`, vision revision `0000009`.
- Storage account: `forenkladevaexsgemhzk7u2`, table `photometadata`, library `owner`.
- Fully exhausted metadata query: uploadDate from 2026-10-10 22:11:00 inclusive
  to 22:20:00 exclusive UTC (2026-10-11 00:11–00:20 Stockholm time).
- Query returned 1,130 unique photo records, matching 1,130 accepted finalizations
  and 1,130 completed worker jobs totaling 1,138,498,944 source bytes.
- Correlated all 56 distinct `productive=False` job filenames against metadata;
  all 51 persisted face failures are among them. Reads are live and nontransactional.
- No task-level errors, exhausted retries, watchdog task timeouts, receive failures,
  or acknowledgement failures were recorded for the batch.

## Persisted outcomes

| Outcome | Photos | Meaning |
| --- | ---: | --- |
| Face done | 1,016 | Accepted complete set of detected candidates; not proof every visible face was detected |
| Face no_data | 63 | Missed detections: zero detector hits despite user-confirmed faces |
| Face postprocessing_failed | 49 | MediaPipe returned no landmarks for one or more YOLO candidates |
| Face quality_filter_rejected | 2 | Backend confidence/size quality gate rejected the candidate |

The 49 landmark failures account for **88 rejected candidates**. Every persisted
reason is `landmark_detection_failed`; there are no recorded landmark exceptions,
invalid landmarks, alignment exceptions, embedding exceptions or malformed
embedding outputs. Forty-five affected photos still saved **59 valid faces**;
four saved none. All 49 have ready detector and landmark models. The four with
no successful landmark result never initialized the embedder in that process;
that is not evidence of an embedding-model loading failure.

The two quality rejections have confidence approximately 0.354 and 0.356. Live
configuration requires confidence 0.36 to bypass the low-confidence size gate.
Their bounding-box area ratios are approximately 18.2% and 23.6%, exceeding the
8% limit; both also exceed the 42% maximum side ratio. These are explicit
policy rejections, not crashes. Both photos saved no faces.

Previews, thumbnails and AI tagging are `done` for all 1,130 photos. OCR is
`done` for 96 and `no_data` for 1,034; these stored statuses cannot establish
whether every no-data OCR result was successful.

## Five non-face flags and diagnostic gap

The other flagged photos are `089616.jpg`, `134007.jpg`, `056297.jpg`,
`142513.jpg` and `148074.jpg`. Each has a successful persisted face result,
preview, thumbnail and AI tagging. EXIF, maps and OCR are `no_data`. No failing
processor exception or per-step error reason is retained for these jobs.

OCR is a likely explanation, not confirmed attribution: `ipwork_ocr.process_ocr`
catches errors and returns `hasData=False` with an `error`. The OCR storage branch
ignores that error, records `no_data`, and does not preserve the reason. The worker
still sees the error and increments `completed_with_step_error`. Native Tesseract/
Leptonica invalid-box messages occur during this upload but lack photo identifiers,
so they cannot prove these five photos' exact failures. Successful server fallbacks
could likewise hide an earlier processor error; the current telemetry cannot
distinguish those cases conclusively.

## Interpretation

The face pipeline intentionally marks an incomplete candidate set as `failed`
while retaining valid faces and allowing their normal assignment to people.
This protects existing faces from destructive replacement. The failed statuses
are terminal for the ordinary recovery sweep; they will not automatically retry
under the current embedding version.

## Implemented recovery and local validation

When explicitly enabled, the face processor tries the independent MediaPipe FaceDetector bundled in
the existing FaceLandmarker task asset when the primary detector/landmarker path
produces no face that passes the backend storage quality gate. Recovery uses the
detector's actual confidence (minimum 0.5), the existing landmark verification,
alignment and AdaFace embedding stages, and the unchanged storage quality gate.
Successful primary results, including partial results, retain their existing
boxes and embeddings. No new model asset or dependency is required.

The shared recovery detector serializes initialization and inference. A missing
model or invalid detector output produces an explicit failure rather than a
successful empty result. Bounded diagnostics record recovery success and the
original candidate counts/scores. No detector confidence is inflated and no
unverified face receives an embedding.

Read-only replay used 146 downloaded original photos: all 114 with detector-zero,
landmark or quality failures, plus 32 successful controls. Inputs were converted
through the worker's preview function before face processing. Models and Python
inference dependency versions match the worker; these local runs were on macOS,
not a deployed Linux revision. Quality settings were explicitly set to the live
`FACE_MIN_STORE_CONFIDENCE=0.30` and `FACE_LOW_CONFIDENCE_REJECT_BELOW=0.36`.

| Local replay result | Baseline | Candidate |
| --- | ---: | ---: |
| Photos with a quality-accepted face | 77 | 142 |
| Photos with no quality-accepted face | 69 | 4 |

All **63 detector misses and both quality rejections** now yield a quality-accepted
face and a verified 512-dimensional embedding. All 77 previously usable photos
(45 partial results and 32 controls) have identical boxes, confidence, alignment
methods and float32 embedding fingerprints across baseline and candidate runs.
This checks detection/embedding preservation; it does not establish recognition
accuracy or complete multi-face recall.

Twelve visually checked background-only crops (clothing, hair, skin and scenery)
produced zero face candidates or embeddings. This limited negative control is
not a general false-positive-rate measurement. The face geometry/recovery,
storage reconciliation and worker diagnostic suites passed 131 tests, including
malformed recovery output and unavailable-model failure handling.
An additional 73 worker batching, face cleanup, upload-boundary and embedding
storage tests passed, for **204 passing targeted tests** in total. After adding
the default-off guard, the repeated targeted suite passed **205 tests**.

The four remaining photos are `037842-ae7f839c.jpg`, `118355.jpg`, `157341.jpg`
and `164481.jpg`. Alternate full-frame and square crops at 1, 1.5, 2 and 3 times
the detected box size, with detector/landmarker detection thresholds of 0.5 and
0.35, still failed landmark verification. Their source poses, occlusion or image
damage need further work. The 45 partial landmark failures also remain partial;
this fix deliberately preserves their usable existing faces.

If these replay results hold after deployment/reprocessing, batch photo-level
coverage would rise from 1,061/1,130 (93.9%) to 1,126/1,130 (99.65%). This is an
expectation from local validation, **not a changed production result**.

The repeatable read-only runner is `backend/scripts/replay_face_pipeline.py`.
Local manifests, originals and reports remain in `/tmp/photostore-face-validation`;
no photo content or embedding vectors are included in the repository report.

## Outstanding work

- Resolve the false positives documented in the negative validation report
  before enabling/deploying recovery or reprocessing affected photos. Their
  current terminal statuses will not automatically trigger a retry.
- Investigate the four remaining landmark-zero and 45 partial landmark outcomes.
- Preserve OCR exception diagnostics separately from successful empty OCR output
  to explain the five non-face processing flags. OCR behavior is unchanged here.

Raw read-only diagnostics were saved locally under `/tmp/photostore-upload-20261011-*`.
The user approved account-scoped Storage Table Data Reader and Storage Blob Data
Reader grants to the current Azure service principal; both were assigned and
verified by successful reads. Application code has changed locally as described
above. No photo metadata, queues or deployment configuration were changed.
