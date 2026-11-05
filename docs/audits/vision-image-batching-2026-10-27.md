# Vision image batching implementation — 2026-10-27

`vision_utils.image_embedding_batches` supplies an optional attempt-scoped broker
around the existing CLIP image encoder. `run_vision_release` enters it before
source adapters are submitted and exits after the source executor has drained.
Existing source ownership, completion, receipt, artifact and publication paths
are unchanged by this feature.

Prepared, oriented/preprocessed tensors are queued; PIL images, source identity,
write guards and persistence functions do not cross into the inference thread.
The broker inherits attempt metrics only. Source context still propagates to
source adapters through the existing `copy_context` calls. Results are associated
by individual futures in submission order, not by image content or filenames.

The queue holds at most twice the configured batch size. Batch 2 and a 2 ms window
are the candidate configuration; a timeout flush uses the original singleton
forward path without a tensor stacking copy. Invalid/failed batched results fall
back to individual inference. Fallback failures reach their source caller and
retain the existing image-encoder failure behavior. Idle workers release batch
references; scope close drains pending work and joins the attempt thread.

## Configuration

- `IPWORK_CLIP_IMAGE_BATCH_SIZE`: defaults to **1, disabled**. Allowed 1–8, capped
  to source worker count. One source worker never incurs a batching delay.
- `IPWORK_CLIP_IMAGE_BATCH_WINDOW_MS`: defaults to 2; allowed 0–50 ms.
- The model, weights, preprocessing, normalization, vocabulary scoring, thresholds
  and singleton numerical path are retained. No extra source workers are enabled.
- The existing Dockerfile already copies new `backend/*.py` modules into the image.

Enablement is conditional on the measured full core result, rather than the
standalone CLIP throughput result. No production environment flag was changed.

## Telemetry

New attempt counters: `clip_image_batches`, `clip_image_batch_items`,
`clip_image_singleton_batches`, `clip_image_batch_fallbacks`, `clip_image_queue_ms`,
`clip_image_stack_ms`, and `clip_image_batch_service_ms`.

Source callers account `clip_image_requests` and `clip_image_request_ms`, including
backpressure, waiting and service. Preprocessing is still accounted on each
source. Under batching, `clip_inference_ms` and `clip_lock_wait_ms` are accounted
once on the attempt by the broker; they are not duplicated onto image counters.
A zero image-level inference counter therefore does not mean CLIP was skipped.
Queue and request durations overlap across concurrent images and must not be
subtracted from wall time or added to infer CPU time.

## Validation

1,247 related regression tests passed. An additional enabled-wave integration
regression verifies that both source threads receive the broker, preserve their
results/normal completion, and do not leak execution/write guards into inference.
The complete wave-source file (29 tests, including that regression) passed.
Focused tests cover singleton flush, exact result association, nested scopes,
invalid batch fallback, exception recovery, concurrent close/drain, telemetry
attribution, opt-out and option bounds. Modified Python modules compile and
`git diff --check` passes.

Azure benchmarking uses the deployed d91e01d image pinned to immutable digest
`sha256:190d9df9a579e8cba1f44cd82f8a15e4041351acfdcde892e3c54cca846967f5`,
a SHA-256-recorded source overlay, 2 CPUs/4GiB, and the same 48 inputs as local.
The temporary job has no managed identity, production configuration or volumes.
It compares the unchanged singleton path with batch 2 at 0/1/2/5 ms windows,
including all OCR/face/CLIP core processing and source-worker scheduling.
The first execution was superseded after discovering that the overlay needed an
explicit `/app/data/tag_vocabulary_embeddings.npz` asset path; it contributes no
performance results. Final measurements and cleanup evidence belong in the
Azure benchmark report.

## Measured outcome

Azure full core wall time regressed from 10.451 s to 10.766 s for 48 inputs at
the candidate 2 ms window. Just three pairs formed across all 768 broker requests
covering four windows and four rounds. Standalone full batches reduce CLIP cost,
but the current synchronous source pipeline does not supply them often enough.
Batching remains disabled by default; no production setting was changed.
Full results: `docs/benchmarks/vision-core-batching-azure-20261027.md`.
Temporary job and private storage were deleted and their absence verified.
