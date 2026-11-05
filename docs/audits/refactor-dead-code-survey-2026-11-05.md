# Refactoring / dead-code survey — 2026-11-05

## Status (updated)

| Item | Branch / commit |
|---|---|
| Dead constants, `_ExploreSink`/`_TimelineSink`, `ImageNameMapping`, MetricCard, blazeface | `codex/prepare-coverage-batching` a5e2eaf (+ MetricCard in d7ea1c9) |
| 4 dead `app.py` imports, Azurite untracked + ignored, `frontend/knip.json` | `codex/dead-code-cleanup` 5757a61 |
| Unreachable legacy UI (`App.tsx` + 21 files, 10k lines) | `codex/remove-legacy-ui` c500018 |
| 16 zero-reference frontend exports (+ orphaned `toShareFile`, `ShareOutcome`, 2 icon imports) | `codex/remove-legacy-ui` (follow-up commit) |
| Still open | §3 remaining unused exports (see below), §4 scripts/audit repros, §5 refactors, §6 shadow-wave code |

Remaining frontend knip "unused exports" are mostly named exports of components whose default export is the one imported (duplicate-export noise), exports used only within their own file, or test hooks (`__reset…ForTests`, `_backgroundRequestQueueLength`); they were left alone deliberately.


Method: `vulture` (backend, min-confidence 60, tests/scripts excluded) and `knip` (frontend), then every hit cross-checked with grep. Both tools produce many false positives here (Flask route decorators, `React.lazy` imports, test monkeypatching of `app.X`), so only verified items are listed. Nothing was changed.

## 1. Safe deletions (high confidence)

> **Update:** applied on `codex/prepare-coverage-batching` (MetricCard, blazeface, dead constants, `_ExploreSink`/`_TimelineSink`, `ImageNameMapping`). Correction: `_perf_*`, `_role_health`, `_wave_install_barrier` are Flask hooks/routes, `tb`/`purge` are parameters, and the `create_video_thumbnail_data` import is patched by `test_ipwork_thumbnail.py`; none of these were dead and the rows were removed.

| Item | Evidence |
|---|---|
| `frontend/src/components/shared/MetricCard.tsx` | 0 references anywhere in `src`/`scripts` |
| `@tensorflow-models/blazeface` in `frontend/package.json` | never imported; `PhotoGallery.tsx` only references the model URL under `/models/` |
| `app.py` constants referenced once (definition only): `TRUST_USER_HEADER`, `MAPS_ENABLED`, `CLIENT_PROCESSING_LATE_RESULT_WAIT_SECONDS`, `CLIENT_PROCESSING_DEFAULT_LEASE_SECONDS`, `FACE_REQUIRE_AI_PERSON_TAG`, `FACE_PERSON_SCORE_THRESHOLD`, `FACE_CLUSTER_LEGACY_EMBEDDING_DIMENSIONS`, `TRASH_SELECT`, `INDEX_BUILD_PEOPLE_MIN_INTERVAL_SECONDS`, `THUMBNAIL_RETRY_COUNT_FIELD` | grep count = 1 across backend + deploy |
| `app.py` classes `_ExploreSink`, `_TimelineSink` | single reference (definition) |
| `storage_utils.py`: `CLIENT_PROCESSING_MAX_THUMBNAIL_BYTES`, `CLIENT_PROCESSING_TERMINAL_STATUSES`, `ImageNameMapping`; `image_utils.py`: `MIN_SIZE_DEFAULT` | single reference |
| `backend/verify_video_corpus.py` | only mentioned in one doc; no importer, no entrypoint |

Before deleting an env-driven constant, check `deploy/` for the matching env var so it is removed there too.

## 2. app.py imports unused in app.py itself — mostly test seams

`app.py:41-80` imports ~15 names it never uses. Tests reference some via `app.X` monkeypatching, so removal needs the tests repointed first.
- Truly dead (0 test/script refs): `resolve_request_user_id`, `build_expanded_query_text`, `get_vector_index_manifest_summary`, `storage_utils_ensure_search_db`.
- Test-only seams (re-export, then fix tests to patch the real module): `get_user_{access,lexical,listing,sort}_index`, `get_{lexical,vector}_index_blob_location`, `list_known_file_hashes`, `vector_search_candidates`, `order_photo_entries`, `index_prime_in_progress`, `_set_face_ids_for_filename`.

## 3. Frontend: probably unused, needs a human decision

- **Correction: `frontend/src/mockups/prototype/` is the live UI** (`src/index.tsx` mounts `PrototypeApp`, commit 90af79d "Deploy prototype as primary UI"). The unreachable code is the *legacy* UI: `src/App.tsx` plus the pages it lazy-loads (`ActivityPage`, `AlbumsPage`, `ChangePasswordPage`, `CorruptedUploadsPage`, `ExplorePage`, `FaceClusters`, `LibraryPage`, `LogoutPage`, `NotFoundPage`, `PeoplePage`, `PersonDetail`, `RecentlyDeletedPage`, `ToolsPage`, `BackendStatusBanner`) and services only they import (`libraryExportDownloader`, `opfsDownloadStaging`, `pagedFetch`, `screenWakeLock`, `serviceWorkerDownload`, `useMultiSelect`, `useThumbnailAccessResolver`). `frontend/knip.json` now models the real entry points and reports exactly these 22 files. Check that `PhotoGallery`/`AppServicesProvider` and their tests are still needed before removal; this is a large deletion (~15k+ lines) and deserves its own branch.
- `frontend/src/benchmark/*`, `vite.benchmark.config.mjs`, `benchmark.html`, `benchmark-build/`: separate Vite entry; verify it is still used by the `/benchmark_*` upload routes before keeping.
- Unused exports (verify, then un-export or delete): `ClusteringActivityIndicator`, `IpworkActivityIndicator`, `setBrowserProcessingTurbo`, `isBrowserProcessingTurboEnabled`, `getBrowserProcessingConcurrency`, `browserProcessingActionSteps`, `getUploadJson`, `postUploadJson`, `getAdmin`, `getCurrentIndexStatus`, `useIndexStatus`, `isClusteringJob`, `isIpworkJob`, `pluralWord`, `isNamedPerson`, `compareNamedFirstThenAlpha`, `encodeUploadDerivative`, `nativeVideoThumbnail`, `YoloFaceDetectionUnavailableError`, `createWorker` (vendor/tesseract), plus 12 unused exported types. Two `__reset…ForTests` exports have no test callers.
- Knip's "unused files" list (App.tsx, all pages, `pagedFetch`, `useMultiSelect`, …) is a **false positive**: they are loaded via `React.lazy`/dynamic import. A `knip.json` with `src/index.tsx`, `src/mockups/main.tsx`, `src/benchmark/main.js`, scripts and workers as entries would make future runs useful.
- Default + named exports on the same shared components (`EmptyState`, `ErrorBoundary`, `Loading`, `Logo`, `dialogs`) — 24 duplicates; pick one style.

## 4. Repo hygiene

- `__azurite_db_blob__.json`, `__azurite_db_blob_extent__.json` (and `__blobstorage__/`) are in the repo root and 2 of those are tracked in git — local emulator state; add to `.gitignore` and `git rm --cached`.
- `backend/scripts/` has 63 scripts; 14 are referenced nowhere (docs, deploy, other code): `backfill_album_token_index`, `backfill_dedup_indexes`, `backfill_embeddings_table`, `backfill_face_by_filename_table`, `backfill_missing_map_geocode`, `benchmark_vision_{buffering,handoff,preview_fetch,scheduling}`, `diagnose_person`, `generate_vector_index_parity_fixture`, `load_test_upload_concurrency`, `prepare_vision_int8_benchmark`, `validate_ipwork_concurrency`. Backfills may be one-shot migrations that already ran — confirm, then delete or move under `docs/` as history.
- `docs/audits/` holds ad-hoc `*.repro.py`, `*.repro.test.ts`, `*.repro.tsx` files from past incidents (album-persistence etc.), which `eslint`/`vitest`/`pytest` may pick up. Consider an `archive/` folder.

## 5. Refactoring hotspots (size/complexity)

| File | Size | Worst offenders |
|---|---|---|
| `backend/app.py` | 18.5k lines, 570 functions | `run_ipworker` 440 lines, `_handle_clustering_queue_payload` 414, `cluster_user_faces` 315, `_run_ipwork_steps_with_images` 291, `_init_storage_clients` 289; ~220 env reads |
| `backend/storage_utils.py` | 12.9k lines, 451 functions | `_apply_client_processing_results` **911 lines**, `_store_client_face_generation_rows` 282, `finalize_uploaded_file` 194 |
| `backend/routes/upload.py` | 1.3k | `finalize_upload_batch` 242, `finalize_direct_upload` 170 |
| `backend/routes/photos.py` | 1.7k | `search_photos` 157, `list_photos` 110 |
| `frontend/PhotoGallery.tsx` | 6.1k | split by concern (viewer, selection, face-detect, upload prompts) |
| `frontend/AppServicesProvider.tsx` | 5.8k | + 1.7k-line upload test; split per service (upload, indexing, browser-AI) |
| `frontend/ToolsPage.tsx`, `AlbumsPage.tsx`, `FaceClusters.tsx` | 1.4–2.3k each | extract panels/hooks |

Suggested order: (1) split `_apply_client_processing_results` (largest, highest-risk function, upload path); (2) move `run_ipworker` and clustering queue handling out of `app.py` into `ipworker_runtime.py`/`clustering_runtime.py` (the latter already exists); (3) move config constants/env parsing from `app.py` into a `config.py`, which also exposes the dead constants in §1 for deletion; (4) split the two giant React files.

## 6. Legacy / shadow code to review

- ~155 `legacy|deprecated|TODO|FIXME` mentions in backend non-test code (concentrated in `index_updates.py`, `library_utils.py`, `processing_completion.py`, `upload_wave_execution.py`, `faiss_assignment.py`, `catalog_metadata.py`).
- `routes/upload_wave_shadow.py` (shadow-mode wave endpoints: open/register/evaluate/resolve/…) and `compare_wave_shadow_to_legacy` in `routes/tools.py`: once the live wave path (`upload_wave_live.py`) has been qualified on this branch, the shadow endpoints and legacy comparison are removal candidates.
- `FACE_CLUSTER_LEGACY_EMBEDDING_DIMENSIONS` (dead, §1) suggests the legacy-embedding path may be removable too.

## Caveats

Not run: the test suite, `ruff`/`mypy`, or any dynamic coverage. Route handlers, Azure/Flask hooks, and anything referenced only by deploy config were treated as live. Always re-grep and run backend + frontend tests after each deletion.
