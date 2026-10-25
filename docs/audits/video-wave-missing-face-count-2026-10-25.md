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
