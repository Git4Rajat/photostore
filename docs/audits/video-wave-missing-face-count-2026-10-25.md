# Video wave blocked by missing face count — 2026-10-25

## Failure

Vision repeatedly failed completion qualification for `dnxhd100_cid1260.mov`
with `Skipped face work with existing faces requires an explicit prior receipt`.
Its older wave held subsequent video and photo waves at the predecessor barrier.

Video's backend processing contract requires no Vision steps. The receipt code
correctly bypassed image face lookup for video, but then tested
`metadata.get('faceCount') != 0`. Missing/null counts satisfy that condition,
raising the same error as a positive count despite no model work being required.
The existing video test inherited `faceCount=0` from an image fixture and did
not exercise ordinary video metadata with the field absent.

## Change

Under the existing source-attempt fence, receipt capture normalizes a missing
or null count to zero only when face processing is outside the required step
set (currently browser-owned video). That value is committed with the receipt
and completion intent, keeping the immutable zero-face manifest, source row,
and subsequent frozen face-coverage check consistent.

Required image face work still needs its qualified lookup. Positive counts,
including video counts, still require prior proof and cannot be discarded.
No face data is deleted, and no completion/receipt validation is disabled.
Existing stored videos can use the normal qualification retry path; this fix
does not require a fresh upload or an initial metadata rewrite.

## Validation

- Before the fix, both missing and null video counts reproduced the exact
  exception; explicit zero passed.
- Receipt regressions check normalized zero in the manifest, completion
  intent, and reference verification, plus rejection of positive counts and
  unproven skipped image face work.
- MOV, MP4, and MXF integration regressions use the real source adapter and
  storage qualification/commit path. They complete the video wave through a
  fake index publisher and promote a waiting photo successor. Model execution
  and original downloads fail the test if called.
- Processing receipts, prepared derivatives, source completion, video uploads,
  wave liveness, and wave consumers: **138 passed**.
- `git diff --check`: passed.

Read-only Azure Table inspection confirmed the live source has
`videoBrowserOnly=true`, `face_status=skipped`, `faceCount=null`, and no
`processingCompletionId`. This matches the regression exactly; the exception's
claim about existing faces is misleading for this record.

Changes are local; no deployment, live repair, or data mutation has been
performed.

## Follow-up after rollout: optional derivative mismatch

Read-only monitoring confirmed revision `0000005` with commit `c22ff07` became
ready. Automatic recovery revived the dispatch at 19:05:28 UTC, and at
19:07:17 UTC the source committed `faceCount=0` with an empty face snapshot
manifest. Qualification then failed with `Artifact manifest does not cover
this completion`: the immutable manifest recorded `thumbnail=done`, while
the committed completion recorded `thumbnail=failed`. Evidence is retained
under `.azure/video-wave-watch/20261025T190548Z.json`.

The proactive follow-up found and fixed three local issues:

1. Result commit required a worker derivative receipt for a browser-owned
   legacy video thumbnail, despite capture and prepared-derivative promotion
   explicitly allowing that output without a worker receipt. Commit now
   preserves those optional video statuses. Explicit receipts still undergo
   validation, and image requirements are unchanged.
2. The rejected completion remains at the head of the durable outbox. Merely
   fixing new commits cannot unblock this persisted state: observation tries
   to relay it before source qualification. Observation now recognizes the
   exact historical video mismatch and permits source requalification.
   Capture retires only same-source, same-generation invalid intents whose
   intact manifest proves the sole difference is an unreceipted optional
   derivative downgraded from done to failed. A fresh manifest and replacement
   outbox commit together under the source fence. Valid receipts, unrelated
   failures, corrupt/missing proof, and other generations are preserved.
   Concurrent outbox changes abort the commit rather than lose evidence.
3. The result-application path captured receipts from a separate candidate
   dictionary but did not copy capture's normalized face count into the result.
   It now commits the exact normalized count and any qualified recovery outbox
   delta together with the manifest reference.

Validation: **586 tests passed** across processing, wave execution/recovery,
publication, and video upload suites; `git diff --check` passed. MOV, MP4,
and MXF tests cover missing counts, successful browser derivatives without
worker receipts, and the persisted rejected outbox. Each advances through
zero-face assignment, publication, an empty retry successor, and release of
a waiting photo wave, without model inference or original downloads. Real
catalog publisher tests exercise all eight scopes with a video present;
sort, access, search, and dedup artifacts include the video filename.

No additional blocker was reproduced in these checks. End-to-end wave tests
use a fake publisher, while separate tests exercise the real catalog
publishers with in-memory storage. Production resolution still requires a
rollout and observed publication receipts; no live repair or deployment was
performed by this follow-up.
