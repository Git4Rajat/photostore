# Library management split — implementation plan

Prepared November 2, 2026. Status: implemented locally; see library-core-archive-rollout.md for migration and release steps.

## Objective

Serve everyday library management from core. Keep library-wide operations and
public-share traffic on archive. Opening Sharing or starting the private app
should not require archive merely to list libraries, members, or invitations.
“Lightweight” means work proportional to management records, not guaranteed
instant responses: storage, email delivery, and password hashing still take time.

## Final route ownership

All paths below start with `/api/library` unless stated otherwise.

| Owner | Routes |
| --- | --- |
| Core | `GET /mine`, `GET /members`, `POST /switch`, `POST /rename` |
| Core | `POST /invite`, `POST /invite/revoke`, `GET /invite/info`, `POST /invite/accept` |
| Core | `POST /members/remove`, `POST /leave` |
| Archive | `DELETE /api/library` (library/account deletion) |
| Archive | `POST /download/request`, `GET /download/status`, `GET /export/manifest` |
| Archive | `POST /clean/request`, `POST /clean/confirm`, `GET /clean/status`, `GET /cleanup-info` and trailing-slash alias |
| Archive | Existing `/public/*` and `/api/public/*` routes |

Existing album management/share-setting endpoints on core remain there. The
public visitor experience and public media remain on archive. Recently Deleted
and its existing restore/purge endpoints remain on core; this work concerns
whole-library deletion rather than individual-photo deletion.

## Findings that affect implementation

- `backend/routes/library.py` currently combines all library endpoints in one
  blueprint. `backend/app.py` registers it only for the archive role.
- Core already has the shared LibraryStore, session signing/verification,
  email configuration, storage access, and per-request membership checks.
  Moving management handlers does not require a new authentication mechanism.
- `frontend/src/services/libraryClient.ts` sends every library request to the
  archive URL. `apiClient.ts` independently classifies every `/api/library`
  path as archive-owned. Both decisions must change together.
- `App.tsx` calls `/mine` during private-app startup, so this move also removes
  an archive dependency outside the Sharing page.
- `LibraryStore.pending_invites()` currently materializes all historical invite
  records in the library partition and filters them in Python. Members listing,
  duplicate checks, and capacity checks depend on it. Filtering status/expiry
  server-side alone would still leave a storage partition scan.
- Account deletion calls `delete_all_invites()` inline, then deletes management
  records and queues the photo purge. It stays on archive.
- With queues configured, the cluster worker executes library cleanup/download;
  archive coordinates the requests. The enqueue helpers can execute inline when
  the library-ops queue is absent, reinforcing the decision to keep these on archive.
- `LibraryPage.tsx` polls cleanup-info every 30 seconds while idle, or every
  three seconds during cleanup. Leaving this unchanged would still wake archive.
- Existing backend tests register all blueprints in the shared test process.
  Isolated role-registration tests are essential to prove actual ownership.

## Implementation sequence

1. **Split the backend blueprints.** Keep management handlers in `library.py`;
   extract deletion/download/export/cleanup into `library_operations.py`.
   Keep request paths, payloads, authorization, and session-token behavior stable.
   Update role dispatch, test fixture registration, and imports. Keep helpers
   shared where they are today rather than moving worker implementations.

2. **Remove invitation history from normal reads before moving their traffic.**
   Introduce an indexed active-invitation lookup with an expiry-bounded key range
   in the existing invitation table. Maintain expiry-keyed candidates and their status on create/accept/revoke and keep
   token lookup behavior intact. Populate existing valid pending invitations in
   an explicit migration on archive/worker, outside core request handlers.
   Enable the indexed read path only after successful backfill; do not silently
   truncate invitations or fall back to historical scans on core. Test expiry,
   lifecycle transitions, and failure/retry consistency. This is the main added
   implementation task, not a prerequisite to inventing new cloud infrastructure.

3. **Centralize frontend ownership.** Add a shared path-to-service resolver used
   by both `libraryClient.ts` and `apiClient.ts`. Explicitly classify archive
   operations, including the method-sensitive root DELETE and cleanup-info alias.
   Management defaults to core; public routes stay on archive. Preserve relative
   URLs, query strings, absolute URLs, and existing single-origin fallbacks.
   Retain the current token persistence for accept/switch/leave.

4. **Stop idle archive polling without losing cleanup notifications.** Include
   a small stored cleanup-status snapshot in the existing core management
   response; read library metadata only, without cleanup reconciliation or scans.
   Refresh that snapshot on focus/visibility and cross-tab cleanup events.
   Contact archive for detailed reconciliation/status only when cleanup is
   pending/active or explicitly requested. Stop polling when it reaches a terminal
   state and pause hidden-page polling. Verify cleanup started from another tab
   is detected, and completed cleanup still produces the existing notification.

5. **Validate and document.** Update role ownership comments and architecture
   documentation. Run the targeted checks below, then frontend typecheck/build
   and the relevant existing backend suites. Keep resource sizing and scaling
   configuration outside this refactor.

## Required checks

- Isolated core/archive route maps, including HTTP methods and negative checks.
- Members, rename, invite/revoke/info/accept, switch, remove, and leave flows.
- Existing session reissuance, owner checks, cross-library isolation, and immediate
  revocation remain effective across services.
- Large historical invite population does not cause management reads to enumerate
  history; valid existing invite tokens survive migration, expiry is respected,
  and active-index updates recover correctly from partial writes.
- Frontend requests target core for management and archive for every operation;
  missing dedicated archive config continues to use the configured API origin.
- Opening Sharing and private-app startup generate no archive management calls.
  Idle Sharing generates no cleanup-info polling. Active and cross-tab cleanup
  detection, failure, completion, and export resumption still work.
- Existing deletion asynchronous-purge, cleanup, export manifest, library download,
  and public album tests pass. A public album remains usable independently of
  the private Sharing page.

## Rollout and rollback

1. Prepare the active-invitation lookup and complete its backfill before directing
   invitation reads to core. Preserve canonical invitation and token rows.
2. Deploy a compatible backend image: core gains management endpoints; archive
   temporarily keeps the same management endpoints for older browser bundles.
   Operations/public remain archive-owned throughout. Deploy archive and core
   independently and verify readiness before switching the frontend.
3. Release the frontend resolver and cleanup polling changes. Verify the network
   destinations and functional flows in the target environment, and observe
   request latency, errors, CPU/memory, and archive wakeups during ordinary use.
4. Retire archive's temporary management registration in a later release after
   the old-client compatibility window; do not remove it in today's cutover.
5. During that window, rolling back the frontend restores old routing. Retain the
   additive invitation lookup and compatible writes until both frontend and
   backend rollback requirements have expired.

## Completion criteria

Today's cutover is complete when management runs on core, whole-library
operations/public traffic remain on archive, invite reads avoid historical
enumeration, idle Sharing does not wake archive, and targeted checks pass.
Compatibility-route removal is an explicitly scheduled follow-up, not a reason
to force existing browser sessions through a breaking deployment today.
