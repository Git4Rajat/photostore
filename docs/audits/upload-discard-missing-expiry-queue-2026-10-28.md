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

## Audit follow-up: cancellation fencing, ancestry and compatibility

The follow-up introduces `/sessions/<id>/discard` as an explicit versioned
operation. New browsers try it first; HTTP 404/405 from preceding upload APIs
selects the old exact-manifest `/abandon` path. Failures to read an older intake
retain the checkpoint rather than treating the intake as absent.

A missing session now receives a durable terminal tombstone under its normal
session key. Its create transaction races atomically with admission: admission
winning first is fenced and cancelled using its real manifest, while Discard
winning first makes late admission return terminal state. Neither branch can
report absence while permitting that session to register files. Tombstones have
no wave and do not increment openSessions. Delayed expiry wakes ignore them.
They remain durable; garbage collection must not remove these fences while old
admission requests or checkpoints could still be replayed.

The browser stops known parent chains, discards the latest parent, then follows
both persisted local links and server-returned `resumedFromSessionId` links.
It retires recovery records only after every ancestor has acknowledged closure.
A failed or interrupted ancestor cleanup can therefore resume after refresh.
Unadmitted retries supply their persisted predecessor hint; the server refuses
to rebind the lineage of an already admitted parent. Accepted source members and
bounded live finalizations retain the existing cancellation protections.

Regression coverage includes both transaction orderings, unleased ancestors,
multiple retries, missing browser manifests, failed ancestor cleanup followed by
refresh/retry, previous-API compatibility and finalization after the discard
fence. These are local code changes; they do not provision the missing live queue
or scaler or deploy a new image.

## Live infrastructure repair — 2026-10-28

At 20:22 UTC the queue was provisioned and cluster revision `0000032` was
latest-ready, running with zero restarts. All three expected queue rules were
verified: clustering and library-ops retain `all`, upload-expiry uses
`visibleonly` with the existing system identity. The existing Storage Queue Data
Contributor role covered the new queue; no role grant was required. Upload
revision `0000027` remained latest-ready.

The final cluster template was compared against the captured baseline: image,
environment, resources, volumes, mounts and identity were preserved; the only
intended template change was the additional expiry scaler. A stable ARM PATCH
initially rejected the derived `imageType` response field. The installed CLI's
scale-rule update then replaced existing rules and emitted empty metadata; this
was corrected by applying the complete verified three-rule template with that
GET-only field omitted. The final template equality check passed.

No user upload, manual wave retry or synthetic expiry message was sent for
verification. Queue existence, exact scale configuration, latest-ready revisions
and worker health were verified; an actual resumed upload remains the end-to-end
confirmation.

The browser's generic waking-up message also hid the structured 503 because the
wave client inspected Axios fields after requestJson had already normalized the
error to ApiError. The local adapter now preserves ApiError.status and
responseData, including the expiry error and the 404 needed by the older-API
Discard fallback. The same adapter recognizes normalized contention errors for
bounded same-payload retries. These browser changes remain local until committed
and built.
