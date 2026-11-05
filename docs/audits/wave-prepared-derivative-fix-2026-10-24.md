# Gallery blocked by missing prepared-derivative receipts

Production symptom: the 32-photo live wave uploaded successfully but Gallery
remained empty. Vision failed its first source commit with
`ValueError: Verified derivative receipt required`; see
[the live inspection](fresh-upload32-live-2026-10-24.md).

## Reproduced cause and fix

Upload finalization accepts the browser's prepared thumbnail and preview into
the existing shared preparation blob locations and marks those steps `done`.
The wave source adapter correctly skips done steps. However, it previously
tried to capture a source-completion manifest without first converting those
prepared bytes into verified immutable derivative receipts. The same failure
occurs on retries where all AI steps are already terminal.

The regression tests invoke the real `wave_runtime.process_source`, lease,
result CAS, artifact-manifest and completion-outbox code, with only image
inference and external storage replaced by fakes. Both the ordinary processing
case and the terminal-results retry failed with the exact production exception
before the fix.

`processing_artifacts.promote_prepared` now reads only the bounded thumbnail and
preview JPEGs using their blob ETags, validates dimensions/content type/bytes,
and publishes create-only immutable copies under the current source/attempt
fence. Existing valid pointers are retained. The normal result CAS commits the
pointers alongside processing results and the completion record. Both result
application and receipt-only recovery use this handoff. Video processing is
unchanged; receipt validation and the gallery publication barrier remain intact.

Tests additionally verify that invalid JPEGs and oversized thumbnails cannot
publish pointers, a source-attempt change during the read prevents publication,
completion manifests validate against the emitted outbox intent, and retries
retain the same immutable pointers even if browser preparation blobs change.
Retries do not rerun completed AI work.

## Verification

```bash
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave*.py backend/tests/test_upload_wave*.py \
  backend/tests/test_processing*.py backend/tests/test_fresh_wave*.py \
  -q --tb=short --show-capture=no
```

467 passed in 4.35 seconds.

```bash
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave_prepared_derivatives.py \
  backend/tests/test_finalize_batch_browser_exif_e2e.py \
  backend/tests/test_finalize_client_blob_name.py \
  backend/tests/test_ipwork_race_guard.py \
  backend/tests/test_ipwork_preview_reuse.py \
  -q --tb=short --show-capture=no
```

49 passed in 28.41 seconds. Five tests overlap the larger run; counts are not
additive. The latter run also checks the final completion-manifest assertion.
`git diff --check` passes. The user's in-progress recovery changes were preserved.

This fix is local and has not been committed, published or deployed. It resolves
the reproduced source-receipt blocker; real clustering, publication and Gallery
refresh still need verification after a compatible rollout and wave resumption.
Respect the existing library's exact-image capability/ownership contract during
that rollout; blindly swapping images or rebinding its certificate is not an
established upgrade procedure. A Tools retry alone cannot fix the old image.
