# Admission failed during transient artifact GC

Live evidence showed `processing_artifact_cleanup` beginning at 20:38:16 UTC
and finishing at 20:38:50 UTC (33.697 seconds). New session admissions during
that interval returned HTTP 409 `Library cleanup has fenced wave mutations`.
The durable policy subsequently had empty cleanupState/gcOwner and gcUntil=0.
This was a bounded artifact reference survey and deletion pass, not a stuck
library deletion. No fence was manually cleared or weakened.

The old API exposed the same permanent Conflict for destructive library cleanup
and short artifact GC. The browser therefore stopped admission immediately.

The fix distinguishes `ArtifactCleanupBusy`, preserving its atomic mutation
fence and returning code `wave_artifact_cleanup_busy`, Retry-After=2 and a useful
waiting message. An early POST preflight prevents known-fenced admissions from
queuing orphan expiry wakes. The atomic policy guard still handles GC starting
after preflight, including partially committed manifest pages.

The browser retries only that explicit transient code with the identical
payload/session/item identities. Backoff is 2, 4, then at most 5 seconds, bounded
by a 90-second cleanup wait budget. Genuine library cleaning, validation errors
and other permanent conflicts retain their existing behavior. A scope change
stops replay. Contention retains its separate three-retry budget.

Regression cases simulate the measured 34-second fence, verify same-payload
replay, prove that a persistent fence stops after the budget, preserve account
scoping and destructive-cleanup behavior, and exercise both preflight and
post-preflight GC acquisition. Validation: 504 backend tests, 128 frontend tests,
TypeScript checking and diff whitespace checks passed. Changes are local until
committed, built and deployed.
