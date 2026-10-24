# Steps 5–7: publication, recovery and idle demand — 2026-10-23

This supersedes the outstanding publisher/source-adapter/scheduler items in the
[execution audit](upload-wave-execution-2026-10-23.md), following the
[decision journal](upload-wave-decision-journal-2026-10-23.md). Changes are local.
No deployment, queue injection, alert activation or live residency measurement
was performed. `live_consumers_available()` remains false, execution is unset
by default, and the optional scheduled job explicitly starts with execution
false. Do not turn on wave execution alongside the unsuppressed legacy pipeline.

## Step 5: actual publication and exact completion

`wave_runtime` routes the new message type through the existing Vision, Cluster
and Indexer worker loops. Each rejects a different app role. Vision uses the
existing model processors, source lease heartbeat and fenced result application;
it does not call the legacy per-photo clustering trigger. Indexer first refreshes
per-photo face metadata, then builds the required projections in prerequisite
order. A normal source barrier wait releases its dispatcher claim without
charging a failure attempt. Real errors still exhaust the bounded retry budget.

The assignment receipt participates in every obligation ID and required-set ID.
The default set covers sort, access, people, albums, streaming search (including
listing, database, Explore and timeline), photo vectors, people embeddings and
the upload-dedup snapshot. Search follows people/albums; people embeddings follow
people. A different assignment receipt cannot satisfy completion.

`wave_catalog` invokes the actual publishers. Serving aliases retain their current
formats. Aliases are pinned before a build; missing aliases use create-only
publication. An upload response's ETag must still match before its bytes are
captured. Every declared output must have been written; swallowed upload errors,
missing builders and incomplete output sets fail the stage. People embeddings
now reject a source scan failure instead of publishing an empty success. The
wave dedup path conditionally commits its delete-epoch witness and manifest in
one hash-table transaction; legacy callers retain their current API behavior.

Published bytes are streamed, preserving stored gzip encoding, into create-only
content-addressed blobs. A stage-fenced proof records immutable blob ETags,
lengths and SHA-256 metadata; the ledger receipt is written only after this proof
exists. A retry verifies the immutable proof before reusing it. Missing/replaced
outputs fail closed. Final completion requires every exact prerequisite/receipt
and the committed assignment receipt. A durable completion can survive a lost
final queue acknowledgement. Publication is not an atomic switch of all serving
aliases: a browsing user can still see different projections during this stage.

## Step 6: independent recovery and operational detection

`wave_recovery` is a distinct scheduled Container Apps **job**, not the existing
request-driven `recovery` app. The latter continues to serve manual admin repair.
The job has no ingress, one replica, a four-minute execution timeout, and a
five-minute schedule. Provisioning defaults off. It uses managed identity and
the existing storage roles; it does no inference, FAISS work or catalog builds.
Importing app initializes the existing storage configuration without starting an
HTTP server or the normal worker polling loops.

A create-only library directory precedes successful new shadow admission. Durable
library/wave cursors bound a run to 20 libraries, two 20-wave pages per library,
and 210 seconds of dispatch-loop budget. Storage request/retry time can extend
that loop budget; the job timeout is the outer bound. Each wave error is logged
and scanning continues. Cursor races fail a run safely; repairs are idempotent.
Recovery promotes eligible successors and reuses the same conditional outboxes
and isolated dispatcher intents as first delivery. It does not reset poison work
or manufacture processing evidence. Vision re-entry relays durable source
completion intents before evaluating the barrier.

Telemetry includes state/wave age, barrier duration, outstanding members/leases,
predecessor identity, handoff destination/age, active stage lease, stage checkpoint
age and failed dispatcher work. Active work is not labeled an unsent handoff.
`deploy/diagnostics/wave-stalls.kql` separates intake, predecessor, source-lease,
unclaimed-handoff and stage-progress failures. Optional `wave-alerts.bicep` rules
cover stalled waves, recovery errors and a missing enabled recovery heartbeat.
Initial budgets: intake 24h, predecessor 2h, barrier/stage progress 30m, handoff
15m, missing scanner heartbeat 30m. Tune against actual library sizes and full
scan cycle time; a 30-minute query window cannot cover a scan cycle longer than
30 minutes. Directory migration/coverage must be verified before activation.

`enableWaveAlerts` and `enableWaveRecovery` default false. Alerts require the
existing logging workspace parameter; no action group is created or notified.
The portal rules need explicit activation after recovery execution is qualified.
No live KQL execution or alert delivery has been verified.

## Step 7: foreground demand and residency qualification

An unattended **visible** page now becomes idle five minutes after real user
input. Upload progress and timers do not renew foreground demand. Job status
polling and browser-processing safety-net polling stop making idle requests.
Push negotiation/reconnect and invalidation callbacks are gated; a response
arriving after the cutoff cannot create a socket. User activity causes catch-up.
Already-running uploads/browser processing continue; closing-browser completion
is not a new requirement. This gate does not claim that every possible product
HTTP request has been proven absent under live traffic.

All eight apps have explicit 300-second scale cooldowns in Bicep: core, web,
upload, Vision, Cluster, Indexer, archive and recovery. Indexer's API container
and queue sidecar still share one replica and scale together. Cluster minimum
must remain zero for this qualification. Scheduled recovery is a bounded
periodic job and is separately accounted for, not a permanently awake app.

`backend/scripts/wave_residency.py` collects all active revisions through Azure
control-plane reads, without waking application HTTP. For each phase, supply the
actual durable phase-entry timestamp and append samples every 30 seconds:

```sh
PYTHONPATH=backend .venv-photostore-tools/bin/python -m scripts.wave_residency collect \
  --subscription SUBSCRIPTION_UUID \
  --group RESOURCE_GROUP --prefix APP_NAME --phase vision \
  --entered-at ACTUAL_UNIX_PHASE_ENTRY >> /tmp/wave-residency.jsonl
.venv-photostore-tools/bin/python backend/scripts/wave_residency.py verify \
  /tmp/wave-residency.jsonl
```

The verifier requires a settled inactive observation in upload, Vision, Cluster,
Index and complete phases, a complete app inventory, zero minima and 300-second
cooldowns. It allows the five-minute tail plus one polling minute; afterward only
the current stage owner may remain. Upload has no allowed resident app while
only direct storage transfer continues. Complete requires all eight at zero.
Use a sufficiently large controlled upload to observe each stage beyond the
tail; a fast local test or short cloud run is not proof. Phase timestamps and
capture completeness still require operator review. Foreground browsing, album/
people CRUD, second-device upload-status reads and their expected idle tails
must also be measured in the deployment qualification.

## Remaining activation gates

- Durable cohort/capability cutover, old release draining and legacy wake
  suppression must be integrated and tested before any execution flag is enabled.
- Existing shadow libraries need directory registration/backfill. Unclosed intake,
  missing/superseded sources and exhausted retries require explicit settlement or
  operator repair; the scanner only detects/redispatches and cannot infer success.
- No-work stage skipping, retention/reference-aware cleanup, source wire-protocol
  migration and destructive-cleanup integration retain their own release gates.
- Large-library memory/disk bounds (including existing People/embedding builders),
  concurrent CRUD/delete/reprocess races, real SDK/queue lease expiry, scheduled
  IAM execution, alert ingestion and measured five-minute residency need a
  controlled cloud qualification. Serving publication remains per projection.

These are release limitations, not a claim that an unqualified flag flip is safe.

## Reproducible validation

**387 focused backend tests passed** (includes retry neutrality, exact assignment
coverage, real catalog publishers, alias races, gzip preservation, delete-epoch
races, missing immutable outputs, scanner fairness and residency verification):

```sh
PYTHONPATH=backend:backend/tests .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_catalog.py backend/tests/test_wave_runtime.py \
  backend/tests/test_wave_residency.py backend/tests/test_wave_recovery.py \
  backend/tests/test_wave_publication.py backend/tests/test_upload_waves.py \
  backend/tests/test_wave_execution_consumers.py backend/tests/test_index_publication.py \
  backend/tests/test_index_work.py backend/tests/test_people_embedding_index.py \
  backend/tests/test_index_maintenance_worker.py backend/tests/test_index_ownership.py \
  backend/tests/test_ipwork_dispatch.py
```

**55 affected frontend tests passed**, and TypeScript checks passed:

```sh
npm --prefix frontend test -- --run src/services/foregroundDemand.test.ts \
  src/services/indexReadyPush.test.ts src/components/AppServicesProvider.upload.test.tsx
npm --prefix frontend run typecheck
```

Full frontend: **689 passed, eight failed** (seven `perf.test.ts` localStorage
fixture failures and the `albumOperations.test.ts` persisted-receipt cleanup
case). All eight identities reproduced against committed HEAD `26cb81e` in a
separate temporary source copy with the same installed Node/dependencies; none
is introduced by this change.

```sh
npm --prefix frontend test -- --run
# On the committed-source copy, using the same node_modules:
npm --prefix /tmp/photostore-frontend-baseline/frontend test -- --run \
  src/services/perf.test.ts src/services/albumOperations.test.ts
```

Bicep builds successfully; regenerated `deploy/azuredeploy.json` matches the
compiled source. Existing name-length, Storage API type-availability and nullable
workspace warnings remain. This is local compilation, not Azure validation,
what-if or deployment. The [Container Apps scale schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/2025-01-01/containerapps),
[job schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/2025-01-01/jobs)
and [alert schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-12-01/scheduledqueryrules)
were checked for the added resource properties.

```sh
mkdir -p /tmp/photostore-bicep-extract
DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/photostore-bicep-extract \
  /Users/rajat.verma1/.azure/bin/bicep build deploy/main.bicep \
  --outfile /tmp/steps567-arm.json
git diff --check
```

Final full backend: **3,725 passed, 23 failed, 28 setup errors, 14 skipped**.
All 51 failure/error identities are in the prior step-4 baseline; no new
identities. One known background lexical-rebuild timing case passed on this run
(the earlier run retained the full 52-case baseline). This does not claim that
unrelated flaky case was fixed. Offline model settings avoid external model
fetch retries during tests.

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=backend:backend/tests \
  .venv-photostore-tools/bin/python -m pytest -q backend/tests
```
