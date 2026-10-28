# Vision artifact handling: local experiment, 28 October 2026

## Scope

The isolated prototype is in `backend/scripts/benchmark_vision_artifacts.py`;
production artifact code is unchanged. It compares the actual current
`promote_prepared`/`publish` implementation against two prototype paths:

1. Reuse the preview bytes already fetched for inference, retaining published
   blob read-back verification.
2. Reuse the preview and create receipts from successful create-only upload
   responses. Request transport content validation; fall back to full read-back
   when the object exists or a success response lacks an ETag. An ambiguous
   upload error fails the attempt; the next attempt verifies any existing blob.

The accounting boundary includes the same initial preview GET in every mode,
two derivative publications, receipt reuse checks and the final source fence.
It excludes OCR/face/CLIP, face/embedding persistence, metadata CAS and completion
delivery. Images/second here means **artifact-path iterations**, not Vision
throughput. Source reads use an in-memory double; blob operations use private
temporary files. Per-call delay is explicitly simulated, not Azure telemetry.

## Inputs and procedure

- 13 real local images, converted before timing into bounded JPEG derivatives.
- Preview payload: 6,537,526 bytes total; both derivatives: 6,579,865 bytes.
- Five rounds per configuration, two source workers, alternating mode order.
- 390 artifact-path iterations across three modes and two delay settings.
- Every output independently checked against its receipt's SHA-256, dimensions
  and JPEG bounds after timing. No images, input names or source paths in reports.
- Local macOS ARM machine; exact platform and individual samples in
  [the JSON report](vision-artifacts-local-20261028.json).

Reproduce with the repository tool Python:

```sh
.venv-photostore-tools/bin/python backend/scripts/benchmark_vision_artifacts.py \
  --corpus /path/to/private/image-directory \
  --output /tmp/vision-artifacts.json --images 24 --rounds 5
```

Only 13 eligible images existed in this run's selected corpus. The script uses
the available inputs rather than silently repeating them to reach 24.

## Measured results

Medians for a complete 13-image artifact-path run:

| Mode | Local wall | CPU | GETs | PUTs | Downloaded bytes |
|---|---:|---:|---:|---:|---:|
| Current implementation | 207.0 ms | 330.3 ms | 65 | 26 | 19,697,256 |
| Reuse preview | 158.5 ms | 253.1 ms | 52 | 26 | 13,159,730 |
| Reuse + upload response | 113.1 ms | 182.2 ms | 26 | 26 | 6,579,865 |

Preview reuse reduced local wall time by **23.4%**. Both changes reduced it by
**45.4%**, and CPU by **44.8%**. Total GETs dropped 60%, downloaded bytes 66.6%.
Including the inference fetch, GETs/image were **5 → 4 → 2**. Promotion alone
was **4 → 3 → 1**; PUTs remained two per image. The remaining promotion GET
loads the thumbnail, whose bytes are not already available to inference.

With **simulated 10 ms per blob call**, median wall times were 815.3, 697.7 and
461.0 ms respectively: 14.4% and 43.5% below current. This is a sensitivity
experiment, **not an Azure prediction**; it does not model bandwidth, SDK retries,
authentication, connection pools or Azure content-integrity enforcement.

The latest Azure wave recorded 3,186 promotion/verification GETs and
1,327,401,139 verified bytes. That evidence establishes repeated artifact work
exists in production; the local experiment establishes which redundant work
the prototype eliminates. It does not establish a whole-wave speedup.

## Correctness validation

`test_vision_artifact_benchmark.py` plus the existing production artifact suite:
**66 passed**. The prototype checks:

- Successful create-only uploads produce valid receipt shapes and matching bytes.
- Retrying existing objects reads and compares them; no overwrite.
- Existing content under a mismatching content-addressed key is rejected.
- Interrupted uploads and lost success responses emit no receipt for the failed
  publication. A retry after a lost response verifies the existing object.
- Missing success ETag falls back to read-back.
- Preview bindings reject changed library/source/revision/file-hash identity.
- A source changed after upload fails the final source fence. Local candidate
  fields/orphan blobs may exist; no durable metadata commit is performed.
- Invalid preview JPEGs cannot complete publication.

These tests deliberately do not exercise final metadata CAS or completion
delivery: neither is changed or implemented by this isolated prototype.

## Recommendation and limits

Preview-byte reuse is the first implementation candidate. Carry an explicitly
bound validated preview envelope from inference to promotion; do not treat an
arbitrary byte buffer or stale mutable preview path as authority. Browser edits,
fallback originals and source changes require the normal validation path.

Upload-response receipts also look worthwhile, but change the proof mechanism:
they trust successful service writes plus transport integrity validation instead
of reading persisted content back. The production `remember_published_derivative`
helper currently explicitly documents read-back proof; that contract must be
updated deliberately if this is implemented, not bypassed implicitly.

Before enabling the second change, run the same variants through the real Azure
Blob SDK against a disposable container, checking returned ETags, content
validation, conflicts, timeout/retry outcomes and exact receipt verification.
This local blob double cannot prove those service/SDK guarantees. Docker was
not running, so no Azurite transport benchmark was performed. No cloud resources,
production state, application code, deployments or model defaults were changed.

Do not extrapolate the 45.4% local artifact-path saving to all Vision processing.
The observed 595 seconds of apply time is accumulated across parallel sources
and includes semantic inference plus other persistence work outside this test.
