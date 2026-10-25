# Scaling includes claimed work — 2026-10-25

The Vision queue represented the 621-member photo wave with one message.
Claiming and renewing that message made it invisible to the `visibleonly`
scaler. Azure system logs show five scale-to-zero events during active work
between 20:12 and 20:40 UTC, followed by queue-triggered activation. Replacement
workers encountered surviving dispatch leases and waited before resuming.

## Change

All four Azure Queue scaling rules now explicitly use `queueLengthStrategy: all`:
Vision, clustering, library operations, and index maintenance. The scale signal
includes claimed messages until ACK. All eight Container Apps retain a zero
minimum; the legacy clustering-floor parameter is restricted to zero. Existing
HTTP scaling, queue targets, maximum replicas, cron rules, receipt renewal, and
execution leases are preserved.

The total queue count is approximate and includes deliberately delayed messages.
Delayed retries and upload-release checks can therefore retain a worker until
handled or expired. This change does not claim to distinguish delayed from active
messages. Queue drain and cooldown permit scale-to-zero; platform, OOM, and
deployment restarts remain possible. See the [KEDA queue scaler contract](https://github.com/kedacore/keda-docs/blob/main/content/docs/2.19/scalers/azure-storage-queue.md).

## Validation and rollout

- Bicep compilation succeeded using the installed compiler. Existing diagnostics
  concern resource schemas, optional workspace references, and string lengths.
- All five deployment regression functions passed when invoked directly. The
  available local Python environments lack pytest, so the full pytest harness
  was not run. Tests cover all eight replica floors and all four queue signals.
- `git diff --check` passed.
- Regenerated `azuredeploy.json` from Bicep. This also reconciles existing source
  drift: the scheduled wave-recovery trigger and missing-recovery alert were
  already present in Bicep but absent from the old compiled template.
- Applied only scale metadata to the live cluster, indexer, and Vision apps in
  `forenklaprep1-dev-rg`. No image build or full infrastructure deployment was
  needed. Verified preserved container image references, all eight zero floors,
  and all four `all` queue rules. New revisions cluster `0000008`, indexer
  `0000005`, and Vision `0000008` became healthy and latest-ready.
- The photo wave had exhausted its stage-dispatch budget during the preceding
  interruptions. With no active stage or dispatcher lease, an explicit
  ETag-protected `retry-stage` request advanced its failed dispatch revision;
  recovery execution `forenklaprep1-dev-wave-recovery-pc5vew0` was started.

Private rollout and progress evidence is under
`.azure/video-wave-watch/active-work-scaling/`. Sustained-work observation is
recorded separately from deployment-readiness verification; readiness alone is
not proof that the entire photo wave has completed or that idle scale-down has
been observed while work remains in the queues.
