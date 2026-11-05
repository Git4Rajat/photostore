# HEIC browser preparation investigation — 2026-10-25

## Report and confirmed bug

The user reported browser thumbnail/preview generation being skipped for HEIC.
The full source corpus decoded successfully, but a separate control-flow
regression was reproduced: image asset preparation ran only when a preview
upload target existed. Thumbnail-only targets started generation for videos,
but not for HEIC/HEIF/HIF (or other still images).

Three new tests failed before the fix because `prepare()` was never called for
HEIC, HEIF, and HIF with only a thumbnail target. The pipeline now runs if
either image asset has an upload target. It generates/uploads the offered
thumbnail and reports the absent preview as `skipped / missing_target`.

This proves a local bypass defect, not the exact target/configuration received
by the user's deployed upload. Access to inspect the live Edge page was not
approved. Disabled frontend/backend preparation and absent targets remain valid
skip paths; the current code records `backend_disabled`, `missing_target`,
`caller_aborted`, or `disposed` where applicable. HEIC itself is not excluded.

## Actual browser validation

Chrome **155.0.8059.86**, headless, with actual local corpus files:

| Samples | Count | Result |
| --- | ---: | --- |
| HEIC: IMG_0127, IMG_0128, iPhone 13 Pro Max, iPhone 15 Pro, Xiaomi, spatial photo | 6 | All generated preview and thumbnail |
| HEIF: Nokia 8.3 5G and HDR | 2 | Both generated preview and thumbnail |
| HIF: Fujifilm, camera sample, rotated camera, Canon, Sony | 5 | All generated preview and thumbnail |

- Native `createImageBitmap(file)` rejected all 13 files. The app's existing
  libheif WASM worker successfully decoded all 13.
- Development worker path: **13/13 generated**, no page errors.
- Production worker bundle under self-hosted script/worker and
  `wasm-unsafe-eval` CSP: **13/13 generated**, no page errors.
- Full preparation pipeline with locally mocked PUT responses: **13/13** had
  generated/uploaded preview, thumbnail, and metadata; browser ExifTool reported
  `done` for all 13. No files were uploaded to cloud storage.
- After the fix, thumbnail-only preparation with the same 13 actual files:
  **13/13** generated/uploaded thumbnails and reported absent previews as
  `skipped / missing_target`, with no page errors.
- Production decode/derivative times were approximately **0.21–2.16 seconds**
  per file. This does not measure real storage latency or mobile performance.
- Sony's metadata fixture is nearly black in both the browser and independent
  Pillow/libheif decoding. The rotated HIF has correct matching portrait
  dimensions/orientation; its browser/backend contrast differs, which is
  separate from preparation being skipped.

## Checks and reusable evidence

- Frontend decoder, asset preparer, pipeline, display routing, and upload
  provider suites: **133 passed**.
- Backend HIF support, thumbnail fallback, and wave derivative suites:
  **22 passed**.
- TypeScript checking and `git diff --check`: passed.
- Reusable harness: `frontend/scripts/smoke-heif-browser.mjs`.
  `HEIF_FULL_PREPARATION=true` checks EXIF and metadata with local mock PUTs;
  add `HEIF_THUMBNAIL_ONLY=true` to exercise the corrected path.
  `HEIF_ASSETS_BUNDLE` selects a production smoke-entry bundle.
- Local result directories: `/private/tmp/photostore-heif-browser-20261025`,
  `/private/tmp/photostore-heif-production-20261025`,
  `/private/tmp/photostore-heif-pipeline-20261025`, and
  `/private/tmp/photostore-heif-thumbnail-only-20261025`.

Safari, Firefox, mobile devices, real storage PUTs, and the user's live session
were not validated. No deployment occurred.

## Follow-up coverage

The thumbnail-only regression also covers JPEG, PNG, WebP, TIFF, CR3, NEF, and
DNG. The previous gate affected still images generally; videos already handled
thumbnail-only targets. Metadata-only targets do not share the defect: ten
image-extension cases verify metadata upload and inline EXIF extraction without
image generation. The expanded pipeline suite has **62 passing tests**.

Backend checks confirm single/batch init can offer a thumbnail without a
preview, both finalize routes accept thumbnail-only results, and wave artifact
promotion preserves the thumbnail while leaving the preview pending. The
preparation routes, wave prepared derivatives, and thumbnail fallback suites
have **40 passing tests**. No backend implementation change was required.
