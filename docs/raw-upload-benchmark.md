# Isolated RAW Upload Benchmark

The authenticated `/benchmark.html` page is separate from the normal uploader.
It uses the existing password session and upload API, with explicit per-file
`benchmark: {runId, arm}` reservations. Enable reservations only in the dev
upload service by setting `BENCHMARK_UPLOAD_EMAIL` to the authorized account.
The capability endpoint checks authentication and active-library membership.
There is no unauthenticated benchmark mode, credential export or CORS change.

## Processing Isolation

Initialization persists `processingIsolated`, `benchmarkRunId` and
`benchmarkArm` before returning storage URLs. Finalization requires the same
reservation, original hash and physical blob identity, retains the marker,
and initializes processing steps as `skipped`. Worker claims, forced enqueue,
recovery scans, browser pending polls, people assignment and on-demand preview
queues reject isolated files. Client derivative application verifies only the
bounded JPEG blobs; it cannot fall back to downloading or decoding the RAW.
Benchmarks do not replace existing library dedup-index pointers.

The marker stays effective when the feature is disabled. Do not roll back the
runtime guards while retained benchmark files exist: older workers do not
understand the marker, and can retry skipped thumbnails. No processing release
operation is provided by this benchmark page.

## Measurement

- Arm A uploads originals and calls real init/finalization APIs.
- Arm B uploads the same originals while one fresh LibRaw WASM worker per file
  sensor-decodes, rotates using LibRaw's orientation policy and creates JPEGs.
- Embedded thumbnails/previews are never requested by the worker.
- Previews have a longest edge at most 2048 px and a strict 1,500,000-byte cap.
- Thumbnails have a longest edge at most 120 px.
- All three assets use direct, write-only Azure Blob SAS URLs. Preview data is
  not base64-encoded in finalization and is not rewritten by the backend.
- Six file lanes, six global storage requests, two block requests per original,
  one sensor worker, 8 MiB original blocks, 12-file API batches and at most 24
  unfinished files are the same in both arms.
- Original extensions remain intact. API rejections are reported per file.
- Sensor failures still upload/finalize the original but send no failure report
  that could activate a server decoder or embedded-preview fallback.
- Verification runs outside upload wall-time and checks original size, JPEG
  encoding/byte/pixel limits, persisted derivative statuses and isolation.
- Success rates describe upload/finalization/verification or sensor generation,
  not OCR, face, tag, orientation or colour accuracy. Timing and size summaries
  include means and interpolated P50/P90/P95 values.

A pilot is required before the full 1,808-file, 53,250,868,221-byte selection
can run. Stop prevents starting more files and drains current transfers and
finalization; completed files stay stored and isolated. Reports exclude tokens
and SAS URLs, are checkpointed in IndexedDB at arm/verification boundaries,
and can be downloaded as JSON. Closing a running page can lose the current
arm's uncheckpointed measurements. The two arms run sequentially, so network
conditions and order remain confounders. Full coverage includes both decoder
failures and API rejections; decoder success uses only sensor-attempted files.

## Deployment

The runtime overlay patch contains only benchmark edits, generated relative
to a pre-edit snapshot. It is applied to the exact running backend/ipworker
image digests at build time; patch mismatch fails the build. The frontend
overlay adds the standalone page, worker/WASM assets and decoder licenses to
the running web image, leaving the existing application bundles untouched.

The deployment branch uses an isolated Git index, excluding unrelated local
changes. Its image workflow retains the existing actor allowlist and `publish`
environment gate, and publishes only a unique benchmark tag, never `latest`.
No new Azure resources, registry access grants or storage keys are needed.

With computer use disabled, browser selection/start and visual checks are
human-operated. CLI build/unit tests do not establish live upload timings.
