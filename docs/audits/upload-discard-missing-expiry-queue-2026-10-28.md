# Discard failed with a misleading missing-photo error

## Verified live cause

The upload and cluster apps were running backend build
`68177ff40fd06e22060e5134794b733a1d815461`, upload revision 0000026 and cluster
revision 0000029. Request telemetry showed failed queue sends to
`photostore-upload-expiry` inside POST `/upload/waves/live/sessions`, followed by
HTTP 409 responses. A read-only ARM resource lookup confirmed `QueueNotFound`.
The cluster app had only its clustering and library-ops queue scale rules.
The new queue and expiry scaler from the committed infrastructure template had
not been applied with the image rollout.

Discard called idempotent session open to obtain the latest manifest revision.
If admission had never succeeded, open tried to enqueue the first expiry check
before creating the intake. The queue SDK's ResourceNotFoundError was caught by
a shared handler that called every missing resource a missing finalized photo.
This explains the reported error without any evidence of a lost photo.

## Code correction

- Discard calls abandonment directly. The server reads the current session and
  manifest revision; it does not create an intake or enqueue expiry to close it.
- An absent session receives an explicit `absent` acknowledgment. The browser can
  retire its local checkpoint after that acknowledgment. Existing sessions still
  use the ETag-protected paged cancellation and preserve accepted members.
- A stale cancellation revision retries from a fresh server read, without going
  through admission or validating a resumed-from ancestor.
- A missing expiry queue becomes HTTP 503 `upload_expiry_unavailable`, with the
  underlying exception logged. Only an actual missing metadata source during
  accepted/duplicate resolution is reported as `Finalized photo not found`.

Live infrastructure repair remains necessary: provision the already-declared
queue and attach the already-declared expiry scale rule to the cluster app.
This code change does not silently create infrastructure during requests or
relax the durable-expiry requirement for admitting new uploads.

Regression cases cover absent intake, preserved accepted files, idempotent
cancellation, a retry whose ancestor is absent, concurrent finalization and
correct separation of queue failure from missing source metadata.
