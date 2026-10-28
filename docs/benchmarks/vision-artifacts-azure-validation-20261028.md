# Azure receipt validation: passed

## Final result

Following explicit authorization, a new private disposable container received
Storage Blob Data Contributor for the current principal, scoped only to that
container. A write probe verified propagation before tests ran. **Validation
passed, the role assignment was removed, and container deletion plus absence of
the exact role assignment were independently verified.** No production blobs,
queues, application tables, settings or deployments were changed.

Used Blob SDK **12.31.0**, Azure Core 1.41.0, Azure Identity 1.25.3, with SDK
retries disabled. Version 12.31.0 matches the deployed core container; Vision was
scaled to zero, so its separate image's package version could not be read.
The SDK was loaded from a disposable directory without upgrading the repo venv.

**234 receipts independently verified:** 13 images, two derivatives, three modes,
three alternating rounds. Every ETag-pinned download matched exact bytes,
SHA-256, size, JPEG dimensions and content type. Independent test downloads are
excluded from the timed candidate requests.

All six additional live cases passed:

1. Identical existing object: create conflict and full read-back safely reuse it.
2. Existing wrong content: conflict plus verification rejects the receipt.
3. Injected failure before send: no receipt/blob; later retry succeeds.
4. Injected lost response after real successful PUT: no receipt from that
   attempt; retry conflicts and verifies the existing object.
5. Invalid Content-MD5 sent to Azure: **400 Md5Mismatch**, blob absent.
6. Blob overwritten after receipt creation: pinned verification rejects with **412**.

Fault injection covers the named transport boundaries, not all network partitions
or arbitrary mid-packet interruption. Missing success ETag and source identity
changes remain covered by local tests. Artifact and source-fence suites:
**96 passed**. The blob harness does not publish metadata/source completion.

Median artifact-path timing for 13 images, two threads, local client to Azure:

| Mode | Wall | Process CPU | GETs/image | PUTs/image |
|---|---:|---:|---:|---:|
| Current | 12.825 s | 1.224 s | 5 | 2 |
| Reuse inference preview | 7.518 s | 0.887 s | 4 | 2 |
| Reuse + upload-response receipts | 5.704 s | 0.589 s | 2 | 2 |

Median artifact-path wall time fell **41% / 56%**. Includes the inference preview
GET; promotion alone is **4 → 3 → 1 GETs**. Models and other persistence are
excluded. All new candidate writes requested transport content validation, actual
PUTs carried Content-MD5, and the negative test confirmed service enforcement.
Current round times varied 8.4–18.0 seconds: three local-client rounds do not
predict a whole-wave improvement on the Azure worker.

The validation gate passes for the tested create-only JPEG upload contract.
Implement behind a default-off flag; preserve source fencing, content-addressed
names, final metadata CAS and full read-back on existing objects/missing response
ETag. Ambiguous upload failures return no receipt; later retries verify content.
Keep the flag off for the first comparison upload, then measure it separately.

The cleanup controller initially used an invalid combination of `--scope` and
`--all` to verify deletion. Role deletion had already succeeded; the corrected
read-only scope query verified absence. Both final cleanup flags are true.

[Live validation results](vision-artifacts-azure-validation-20261028.json) and
[container-scoped access/cleanup proof](vision-artifacts-azure-access-20261028.json).

## Historical initial access preflight (superseded)

The following records the earlier failed attempt, before the user authorized
the container-scoped grant. It does not describe the final validation state.

28 October 2026. Validation harness:
`backend/scripts/validate_azure_artifact_receipts.py`.

The first live attempt created a private disposable container, verified that it
was private, then received **403 AuthorizationPermissionMismatch** on the first
test blob PUT. No receipt validation or performance case executed. The harness
deleted the container and verified 404 on a subsequent container-properties read.
The JSON report above now contains the successful rerun.
This is **not a passing SDK validation** and does not justify enabling
upload-response receipts.

Current principal `73312e38-b31d-4f16-bab1-a51e5a22caf6` has table data access
and queue sender assignments but no account-specific Blob Data Contributor
assignment. No permission changes, production blob changes, app configuration
changes or queue sends were made by this test.

Read-only `pip show azure-storage-blob` inside the running core container
(`forenklaprep1-dev-core--0000023`) reported **12.31.0**. Vision was scaled to
zero, so its package version could not be read directly. Local environment was
12.30.0 for the rejected attempt. SDK 12.31.0 was prepared in a disposable target
directory for the eventual rerun; the repository environment was not upgraded.
The core container is a useful build comparison, but does not independently
prove the installed SDK version of the separate Vision image.

To proceed with the current principal, authorization is needed for a
**Storage Blob Data Contributor** assignment scoped only to a newly created
test container. Remove that assignment and delete the container after the run.
The harness itself intentionally does not grant or modify permissions.

Prepared cases include independent ETag-pinned byte/hash/dimension verification,
identical create conflict, wrong existing content, injected failure before send,
injected lost response after a real successful PUT, retry verification, real
Azure rejection of an invalid transport MD5, and stale ETag rejection after blob
mutation. The transport cases are deliberately distinguished: they do not claim
to emulate arbitrary network partitions or mid-packet interruption.

Source revision fencing remains covered by the separate local correctness tests;
the standalone blob harness does not read application tables or publish source
completion.
