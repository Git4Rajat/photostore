# Automatic new-library enrollment enabled in dev — 2026-10-24

The user explicitly requested enabling enrollment before inviting a new user
with their own library. Deployment `wave-enrollment-20261024` targets the existing
`forenklaprep1-dev-rg`, subscription
`c14d0e72-4b72-429d-bf2a-8cede087b9a8`, West Europe.

Only `UPLOAD_WAVE_NEW_LIBRARIES_ENABLED` changed from `false` to `true` in the
Core and Archive containers, the two library creators. Both ready revisions are
now `--0000013`. The other six apps and scheduled recovery job were not deployed.
The existing images from `ff12cf0`, source/execution fences, capability
`49cc07dae8347bc64bb3b29191d8746a6e2ac99c8dae28dbf52488223bc38764`, secrets,
principals, resource limits, ingress, mounts and scale settings were preserved.
No resources or role assignments were added or deleted.

Fresh live drift checks against the previously validated deployment passed.
The small two-app Bicep overlay compiled, provider validation and what-if passed
under the existing policies. Machine-readable what-if showed exactly two
modified app resources and no creates/deletes. Post-deployment comparison passed,
including fresh secret-value equality and unchanged remaining six apps. The
read-only qualification collector reports `configurationReady=true`, no
configuration blockers, and `cloudQualified=false`; configuration readiness is
not full functional/reliability qualification.

## Invitation behavior

Select **Use the app fresh (their own new library)** in the library invitation
form. The `fresh` invitation acceptance route creates the new account's personal
library, leaves it independent of the inviter's library, and selects it for the
returned session and subsequent normal login. The normal new-library callback
now enrolls that library automatically; no per-library operator enrollment is
required. Existing libraries are not migrated by this flag change.

The actual recipient's invitation was not sent by the agent; the user intends
to send it. A separate disposable, expiring `fresh` invitation was seeded using
the existing LibraryStore and app identity, without email, solely to exercise
the normal HTTP acceptance path. That test does not validate email delivery or
the browser UI.

## Evidence and rollback

Private inputs, captured secrets, validated parameters, rollback parameters and
verification reports are retained under ignored
`.azure/wave-enrollment-20261024/`. The verification commands are:

```bash
.venv-photostore-tools/bin/python .azure/wave-enrollment-20261024/verify.py
.venv-photostore-tools/bin/python .azure/wave-enrollment-20261024/accept_invite.py
```

The acceptance script refuses a duplicate invocation after account creation.
Restore only Core/Archive enrollment flags to `false` to stop further automatic
enrollment. Libraries already enrolled retain durable wave ownership and must
continue with compatible workers; disabling enrollment is not a legacy fallback.
Future full infrastructure deployments must explicitly retain
`enableNewWaveLibraries=true`, enabled wave execution and the same compatible
capability/image bindings; the template's opt-in default remains false.

This activation follows the [parallel multi-user test](upload-wave-multiuser-2026-10-24.md).
The renamed-upload hash-tracking omission and remaining UI/CRUD/900+/fault/load
qualification are still recorded limitations. No source fix for those was
included in this configuration-only activation.


## Acceptance check passed

The real Archive invitation-acceptance endpoint created disposable personal
library `uf1967c3bc6b8446cac120101f85b4c17` with a durable `library-wave-v1`
ownership policy bound to the existing capability. The account has exactly one
library, owns it, and has no membership in the inviter's library. Normal password
re-login retained its own active library. Upload protocol discovery returned
`live`. A one-item cancelled intake completed wave
`036119524a1c4a4ba30a8d05ca5ca5f4` with `outcome=no_work` through normal API
advancement. No enrollment flag override, direct ownership write, synthetic
completion receipt or email was used by the acceptance test. The preceding
multi-user test supplies positive Vision/clustering/indexing evidence; this
acceptance check specifically verifies automatic enrollment and admission.
