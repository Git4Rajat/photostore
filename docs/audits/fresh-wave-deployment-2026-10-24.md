# Fresh resource-group wave deployment readiness

Scope: user chose `deploy/sandbox.bicep` / Azure CLI and will recreate the
resource group. No Azure resources or library data were changed by this work.
No existing-library migration or certificate rebind is provided.

## Changes

- Publication emits the three image digests from one source commit, runs wave
  unit qualification inside that exact backend image, then emits
  `wave-release.json`. Failed qualification produces no release artifact.
- `deploy/fresh_wave.py` prepares explicit private parameter files by default.
  `--deploy` validates/provisions with intake blocked, runs certificate seeding
  through an execution override of the existing recovery job, activates only
  after successful seeding, and verifies actual images/configuration/readiness.
  Resume pins the entire release, owner email and deployment target.
- Bootstrap uses the runtime's content-addressed eight-role capability contract.
  It rejects existing library/metadata/ownership rows, verifies its persisted
  certificate and retries transient storage/RBAC propagation failures within a
  bounded job execution. It is explicitly invoked, not worker self-certification.
- During preparation, HTTP APIs except health return 503, owner-library bootstrap
  is deferred, and Vision/Cluster/Indexer worker entrypoints wait without imports
  or queue consumption. The scheduled recovery command remains `wave_recovery`,
  disabled until activation; only the explicit execution runs `wave_bootstrap`.
- Activation supplies backend processing, the exact capability ID, execution and
  enrollment flags, source completion/generation enforcement, dedicated Indexer
  sidecar and enabled scheduled recovery. The final authenticated probe requires
  the new owner to use `live` and a cancelled session to finish `no_work`.
- Compiled `azuredeploy.json` was regenerated, including the earlier commit's
  backend-processing/wave defaults that had not reached the generated template.
  Portal UI processing default now agrees with Bicep. Portal deployment does not
  perform the fresh CLI bootstrap; the README explicitly directs wave installs to
  [the CLI guide](../../deploy/fresh-wave.md).

No additional bootstrap resource, identity, grant or continuous coordinator is
introduced. The existing recovery job's storage-scoped roles are reused.

## Local verification

```bash
PYTHONPATH=backend .venv-photostore-tools/bin/python -m pytest \
  backend/tests/test_wave*.py \
  backend/tests/test_upload_wave*.py \
  backend/tests/test_fresh_wave*.py \
  backend/tests/test_library_utils.py -q
```

**339 passed**, 4.10 seconds. Includes certificate/runtime digest agreement,
freshness/ownership refusal, bootstrap retry idempotency, owner enrollment order,
HTTP and worker barriers, wrong-image/blank-certificate/missing-sidecar rejection,
legacy-protocol refusal, private-file preparation without Azure calls, refusal of
an existing resource group, full-release resume pinning, and mocked successful/
failed bootstrap orchestration proving activation is gated on job success.

```bash
az bicep build --file deploy/sandbox.bicep --outfile /tmp/photostore-fresh-sandbox.json
az bicep build --file deploy/main.bicep --outfile deploy/azuredeploy.json
bash -n backend/entrypoint.sh
.venv-photostore-tools/bin/python -c 'import yaml; yaml.safe_load(open(".github/workflows/publish-images.yml"))'
python3 deploy/fresh_wave.py --help
git diff --check
```

All succeeded. Bicep reports existing warnings: communication-service name
minimum length (BCP334), missing local type definitions for Storage API
2025-06-01 (BCP081), and nullable conditional Log Analytics workspace accesses
(BCP318/BCP422). These are compiler results, not Azure provider validation.

## Still required before claiming deployment success

1. Commit/publish these changes; download the successful run's `wave-release`
   artifact. The new CI qualification job has not run in GitHub yet.
2. Prepare the real deployment plan with the owner email/password. Operator
   removal of the old group remains separate; the helper never deletes it.
3. Run `--deploy --resume` against the absent group. This performs provider
   validation, both deployments, real certificate seeding and admission smoke.
   Azure job override syntax, identity propagation, ready revisions and real
   storage/owner bootstrap have not been exercised by this local test pass.
4. Qualify real images on the owner and another user's new library: upload,
   overlapping sessions, Vision, one clustering pass per wave, complete
   publication coverage and gallery refresh. A cancelled-session smoke is not
   evidence for those paths, email delivery or residency timing.

An activation/verification failure may leave apps active. Resume checks the
same release and target; the helper does not promise cross-app Azure rollback.

Related investigation: [latest upload LAW audit](latest-upload-law-2026-10-24.md).
