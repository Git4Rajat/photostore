# Library members 503 — November 2, 2026

Resolved missing invitation-index initialization in the existing development
storage account `forenklaprep1devmqwhngus` at approximately 17:14 UTC.

## Evidence

Core and Archive revision `0000051` were healthy, active, and receiving all
traffic. Both used backend image
`0b767f9c27d01b9010ee3a2d50db8ece6dcab489`; all listed backend applications
used that source version. Its invitation writers maintain canonical and
indexed records atomically.

Core logged authenticated `GET /api/library/members` failures at 17:11:42
and 17:11:53 UTC, returning 503 in 47–66 ms. Account, membership, and library
reads succeeded; the invitation table point read failed. Both existing
libraries lacked `_pending_index_v1`. Core deliberately returns 503 in that
case rather than scanning historical invitations.

The read-only audit found zero unexpired pending invitations in either library.
The invitation table contained one historical accepted invitation and no
readiness markers. The frontend had already been cut over before the required
migration in `docs/library-core-archive-rollout.md`.

## Repair and verification

Ran the existing migration with the verified storage account:

```bash
STORAGE_ACCOUNT_NAME=forenklaprep1devmqwhngus python3 backend/scripts/backfill_invitation_index.py --all
STORAGE_ACCOUNT_NAME=forenklaprep1devmqwhngus python3 backend/scripts/backfill_invitation_index.py --all --apply --writers-upgraded
```

Both libraries reported ready. This initialized two version-1 readiness markers;
there were no pending lookup records to add. The migration preserves canonical
invitations, account data, memberships, and tokens. No application deployment or
infrastructure configuration change was needed.

Read back both markers and successfully executed live
`LibraryStore.pending_invites` with `require_invite_index=True` for both libraries;
each returned an empty pending list. Invitation-index and library-management
route tests passed: 12 tests using `.venv-photostore-tools/bin/python`.
An authenticated HTTP request from the user's browser was not replayed; live
verification covers the exact storage operation that caused the 503.
