# Uncommitted live person albums audit — 2026-10-22

**Three actionable findings: two P1 issues and one P2 issue. Four added adversarial reproductions fail.**

Scope: all current uncommitted application changes: backend/app.py, backend/routes/albums.py, backend/routes/people.py, backend/storage_utils.py, and the new backend/tests/test_live_person_albums.py. The earlier indexing/UI changes have been committed and are not this diff. No implementation or existing test files were edited during this audit. User-owned emulator files were untouched.

## 1. [P1] Background resync can restore sharing settings after the user revokes access

Location: backend/app.py:8898–8899, also the full-entity saves in the new redirect and rename helpers.

The live-album query loads full album entities before resolving person faces and computing membership. _save_album_entity then upserts that entire old entity without an ETag condition. If the user revokes sharing, changes the access code, renames, or deletes the album during the intervening work, the resync writes the old values back with its membership change. This is a new automatic background writer racing ordinary album actions.

Reproduced deterministically: start with a public album and its old token; after resync has captured the album row, persist a revocation (isPublic=False, publicToken=''); complete face resolution with one additional matching photo. The new photo is added, but the saved album becomes public again with the old token. The assertion expecting isPublic=False fails with True. The public lookup also falls back to finding an album by token when its token-index row is missing (backend/app.py:_find_public_album_by_token), so deleting that index row during revocation does not prevent the restored token from being found.

Update only the intended fields against an ETag, refetching/recomputing on conflict. Recheck deletion and rule ownership before publishing. Apply equivalent concurrency protection to redirect and automatic rename, and protect membership computation against older concurrent resyncs overwriting newer results. A process-local lock alone does not protect against other replicas.

## 2. [P1] Temporary read failures are interpreted as an empty person and remove album contents

Location: backend/app.py:8875–8884.

Every exception from the person read or faceIds parsing is converted to face_ids=[]. The code then treats this as authoritative empty membership, removes all photos, saves, and dirty-marks the album index. Timeout, throttling, or service unavailability is therefore indistinguishable from a confirmed deleted person. person_table_client=None has the same outcome. Filename resolution also uses an existing best-effort helper that can omit unresolved face IDs, so incomplete resolution must not authorize removals either.

Reproduced: a valid person and album contain a.jpg; inject a temporary RuntimeError from the person table read; invoke resync. The persisted album contains [], expected ['a.jpg']. The source person still exists.

Only a confirmed not-found result should be eligible for the deleted-person behavior. On unavailable or incomplete source data, preserve membership and arrange retry; use strict, complete face-to-filename resolution before destructive set reconciliation. Distinguish a failed album query from a confirmed absence as well so redirects are not silently lost.

## 3. [P2] Live membership is not synchronized after several ordinary face edits

Location: new synchronization hooks in backend/app.py:6177–6178 and backend/app.py:5808–5812; missing corresponding post-mutation synchronization in the face confirmation/rejection/deletion paths.

The new hooks cover automatic assignment, reclustering, merges, and some indirect removals, but do not cover all mutations to person faceIds. confirm_face and accept_suggested_faces add to the destination via _add_face_to_person without a final resync. Their old-owner removal can run before _remove_face_from_other_people scans, so the latter does not reliably resync that former owner either. Bulk rejection, not-a-face, person deletion, and splitting also have direct mutation paths that do not invoke the new synchronization. Rebuilding an index does not recompute album membership.

Two independent actual-code reproductions fail:

- Reject f2 from A, leaving A with only f1/a.jpg. _delete_faces_bulk reports successful rejection and the person row is correct, but its live album still contains a.jpg and b.jpg.
- Confirm A's f1/a.jpg into B. The actual confirm-face route returns success and B's faceIds contain f1 and f2, but B's live album still contains only b.jpg, missing a.jpg.

Schedule or invoke synchronization for every successfully affected old and new person after source mutations commit. Prefer one shared, retryable integration point so per-route lists of hooks cannot drift apart. Include remove-last-face, person deletion, suggestion acceptance, split, and propagation's former owners in regression coverage.

## Validation and artifacts

- **34 tests passed** across live person albums, merge core, label indexes, and dirty People coverage.
- **159 tests passed** across album storage/indexes/public sharing, label confirmation, People index, maintenance workers, and index events. Combined selected baseline: **193 passed**.
- **Four added adversarial tests failed**, exposing the three findings above. Tests use actual resync/helper/route code with controlled in-memory storage; they do not call live Azure services.
- Changed Python files parse successfully and git diff --check passed. Frontend was unchanged, so its test/build suites were not rerun.

Reproduction artifact: docs/audits/live-person-albums-2026-10-22.repro.py. Copy it to backend/tests/test_audit_live_person_albums.py, then run `.venv-photostore-tools/bin/python -m pytest backend/tests/test_audit_live_person_albums.py -q`. It imports the new live-person-album test fixtures. The temporary executable copy was removed after the audit.

Behavior choices observed, not classified as defects: only newly auto-created person albums get rule fields; manual membership edits are overwritten on resync; undo-merge does not reverse rule redirection; redirected albums keep their prior name until an explicit person rename. No browser E2E, deployment, full backend suite, or live storage concurrency run was performed. No fixes, staging, commits, or external messages were performed.
