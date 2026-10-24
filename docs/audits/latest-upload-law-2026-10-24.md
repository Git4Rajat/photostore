# Latest 18-photo upload: LAW inspection — 2026-10-24

Read-only inspection of `forenklaprep1-dev-law` in
`forenklaprep1-dev-rg`, requested by the user after a new build/deployment.
Private raw queries are under ignored `.azure/wave-latest-20261024/`.
No app, configuration, role assignment or user data was changed.

## Observed result

During 13:25–13:45 UTC (15:25–15:45 Stockholm), Upload request telemetry records
seven successful init-batch requests and fourteen successful finalize-batch
requests, all HTTP 200. No HTTP >=400 appears in the returned request summary.
This is evidence for this small run, not proof that the former 5,000-file
contention case is fixed at representative load.

Vision's replica counters report 18 received, 18 done, 18 acknowledged,
18 productive completions, zero task errors, zero completions with step errors
and zero acknowledgement failures. The earliest returned periodic summary with
18 completions was ingested at 13:36:34 UTC. Preview reuse count is 18. Counters
alone do not establish face-assignment quality or publication receipt coverage.

Indexer logs show sort and access indexes built with 18 rows. They also show
multiple earlier builds during intake, with sort row counts 10, 11 and 18.
Cluster logs show `ipwork_release` and `ipwork_release_check` jobs for `owner`;
no actual single upload-wave clustering execution was established.

## This did not qualify the wave algorithm

All correlated legacy worker jobs and index output name library `owner`.
The upload logs reference wave partition
`upload-wave-shadow-622f06cbcd2e7e61637e17212e714e6c721d2dd5a9f7bc1aec911b65b34edcf5`,
which equals the protocol digest of `owner`. There is protocol discovery, but
no live wave session open/register/resolve/close request in the returned window.
Together with legacy release jobs and incremental indexing during intake, this
indicates the run followed the existing owner's legacy processing path.

New-library enrollment does not convert this populated existing library.
Multiple apps log `Owner account bootstrap failed: Wave enrollment requires a
new empty library`, consistent with an attempt to enroll the populated owner.

Live ARM configuration now shows all backend containers with execution and
new-library enrollment flags `true`, but **`UPLOAD_WAVE_CAPABILITY_ID` is empty**.
Images are commit tags for `281b53b684dbd6c3b787d206f55ec3bdef8880b2`, rather
than the previous pinned digest configuration. This is observed deployment drift
from the earlier qualified capability, not a configuration change made here.
Setting execution flags alone cannot establish valid wave ownership/admission.

The next valid wave test requires a durable certificate bound to the actual
role images, matching app/job environment configuration, and a fresh enrolled
library (or a separately designed, explicit migration). Previously enrolled
libraries also require compatible ownership handling; do not silently change
certificate identities to bypass that requirement.

An attempted operator Table read returned `AuthorizationPermissionMismatch`.
No grants, keys, alternate app execution or policy writes were used to bypass
that denial. Durable source/assignment/publication receipts were therefore not
verified. The result is a successful-looking legacy processing run with a wave
configuration blocker, not a passed one-shot wave qualification.
