# Revoke/reinvite investigation — 2026-10-24

The user reported that first invitation emails arrive but replacements after
revocation appear not to. This was investigated against the existing dev Archive
request logs, email acceptance logs and already-enabled ACS send/delivery
operational tables. No messages were sent, no invitations were revoked, and no
live state or deployment was changed during this investigation.

## Observed evidence

Times below are UTC (Stockholm was UTC+2):

- 08:09:43: send returned HTTP 200; ACS message
  `02c252bb-c9ba-450f-8f73-775563dbc380` has a `Delivered` result to a Gmail
  recipient. The mail was subsequently revoked at 08:09:45.
- 08:09:55: another send returned HTTP 429, 12 seconds after the preceding
  send. This is consistent with the existing 30-second per-library cooldown.
- 08:10:26: send returned HTTP 200; ACS accepted message
  `95957fdd-6fee-469e-81df-346cb189500d` with operation status `Succeeded`.
  The queried ACS operational tables contain no matching terminal delivery row;
  arrival cannot be confirmed from available telemetry.
- 08:15:14: send returned HTTP 200; ACS message
  `db607553-330c-4a36-88c4-d12c80643896` has `Delivered` at 08:15:18.551 to an
  iCloud recipient, with no failure reason or hard bounce.

`Delivered` is recipient-server acceptance, not evidence of inbox placement.
Mail threading or spam filtering may explain a missing visible message, but
those are hypotheses, not observed root causes. ACS operation `Succeeded` alone
also does not prove delivery. Different send records need not have the same
recipient; logs were reviewed with recipient addresses redacted.

## Code and regression check

`LibraryStore.revoke_invite` sets only that invitation's status to `revoked`
and removes its token lookup. `find_pending_invite_for_email` excludes revoked
records. A replacement gets a new token and new row. Revocation intentionally
does not reset the library's email cooldown or its 30-per-hour cap.

Added a deterministic regression proving: revoke removes the pending invitation;
an immediate resend remains throttled; a resend at the 30-second boundary is
allowed; the replacement resolves; the old token remains invalid; accepting the
replacement consumes it without reviving the old token.

```bash
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_library_utils.py backend/tests/test_membership_flow.py -q
```

Result: **23 passed**. `git diff --check` passed. This uses in-memory store tests
and does not claim end-to-end recipient mailbox validation. No production logic
was changed, and the test addition has not been committed or deployed.

Operational guidance: wait at least 30 seconds from the last send before
reinviting, use the newest message/link because revocation invalidates the old
one, and inspect the existing mail conversation/Junk if the provider reports
delivery. The later accepted-but-unconfirmed message remains explicitly unknown.
Private query evidence is under ignored `.azure/wave-enrollment-20261024/`.
