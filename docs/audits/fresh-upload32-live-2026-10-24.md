# Fresh deployment and 32-file live upload inspection

Read-only inspection on 2026-10-24, approximately 16:42–16:51 UTC
(18:42–18:51 Stockholm). Resource group `forenklaprep1-dev-rg`, workspace
`forenklaprep1-dev-law`, web environment `blackriver-d7830f56`.

## Deployment configuration

Actual ARM app/job resources pass `verify_inventory` from committed
`fe47e54:deploy/fresh_wave.py` against `.azure/fresh-wave-rebuild2/state.json`.
All eight apps have ready latest revisions and minimum replicas zero. Indexer
includes the separate `index-worker` sidecar and queue scaler. Backend processing,
execution, new-library enrollment, source completion and generation enforcement
are enabled; bootstrap pending is false.

- Backend digest: `dab38faa94b889b174adc2bc121d5c40f10d0f42093bb41db45d4db8330607fc`.
- Vision digest: `10fe6e264829516469a6d4dac4b24e929b192dcc6f91d9880379b68138b90b80`.
- Frontend digest: `4792de1f154617f6d87016fe6becd78e46bdb9690c165e1299d94050f83d163c`.
- Capability: `4b9acf83b3f82162335aec0448e8e77de22570b0e4e868e3fd7a3304a71047d3`.

Recovery runs the matching backend image and `python -u -m wave_recovery`, with
a five-minute Schedule trigger. Recent scheduled executions succeed; the job has
the four intended Blob Contributor, Queue Contributor, Table Contributor and
Blob Delegator grants on the photo storage account. It has no permanent replica.

The working tree already contains newer, uncommitted Manual-recovery changes.
Those were preserved. Live matches the pushed commit's scheduled recovery,
not that newer local design. Vision also retains the template's periodic sweep
cron scaler; min-zero configuration alone does not prove indefinite idle sleep.

## Upload and runtime outcome so far

Normal owner authentication and the read-only protocol/status endpoints confirm
`live`, one session, 32 members and zero open sessions. Wave:
`b9f7097f83094ec79d507a31b34ada10`; session:
`3b58e74e-e6e4-42f0-a203-0374f0c2c0e8`.

Browser batch telemetry reports 32 uploaded, zero failed, zero duplicates,
154,076 ms elapsed. Returned HTTP telemetry during intake shows successful live
open/register/resolve/close and finalize calls, with no HTTP >=400 in the
queried window. Requests/retries are not unique-photo counts.

Intake closed around 16:45:08 UTC. Recovery observed `processing_pending` with a
Vision handoff, no dispatch failure, and a satisfied predecessor (the bootstrap
no-work smoke wave). Vision scaled from zero around 16:45:12, waited for Azure
infrastructure, started its main process around 16:48:41 and became worker-ready
around 16:48:57. Initial infrastructure waiting resolved without container
restarts; it is distinct from the subsequent application failure.

At **16:48:58 UTC**, the first processed source, `sample_001.srw`, failed:

```text
wave_runtime.process_source
  -> storage_utils.apply_client_processing_results_for_file
  -> ArtifactReceipts.capture
  -> verify_receipt / validate_receipt
ValueError: Verified derivative receipt required
```

The failure occurs while capturing the source artifact receipt after processing
results, not during transfer or initial model loading. `capture` requires verified
thumbnail/preview receipts for done derivatives; `validate_receipt` rejects a
missing derivative pointer. The traceback does not identify which derivative is
missing, so that must be established before selecting a fix. Do not weaken receipt
validation to make the wave advance.

At 16:49:58, Vision's throughput summary reports one error, zero done and zero
acknowledged. No successful one-shot clustering or complete publication coverage
has been demonstrated. This is a correctly configured deployment with a runtime
receipt-integration blocker, not a passed end-to-end qualification.

Core also logged missing People-index blobs during initial empty-gallery startup;
Indexer built empty indexes before upload. The queried HTTP summary shows no
corresponding failed HTTP requests. Those startup logs are separate from the
explicit Vision receipt exception above.

## Evidence and continued observation

Private evidence is under ignored `.azure/upload32-watch/`: actual ARM inventory,
request/event summaries, authenticated wave status and `vision-first-error.json`.
Operator Storage Table access returned a permissions denial. No keys, app
identity impersonation or role changes were used. The owner's normal API access
was used only for protocol and wave-status reads; no sessions were created or
repaired by this inspection.

Heartbeat `watch-32-file-upload-wave` checks every five minutes, stays quiet on
unchanged observations, and reports meaningful progress, distinct failures,
retry exhaustion or completion. It should disable itself after reporting the
wave's final outcome. No deployment, repair or configuration changes were made.
