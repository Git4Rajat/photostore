# Parallel multi-user dev qualification — 2026-10-24

This extends the [fixed single-user canary](upload-wave-fixed-canary-2026-10-24.md)
against the same deployed images from `ff12cf0` in `forenklaprep1-dev-rg`.
General new-library enrollment remains disabled. Only four fresh disposable
accounts/personal libraries were explicitly enrolled through the normal policy
checks using the existing app managed identity. User B was added directly as a
non-owner member of A's library; no invitations or email were sent. Existing
users and photos were not changed. No deployment or scale configuration changed.

## Test shape

Four independent authenticated HTTP clients ran in a Python thread pool. Each
opened a two-item upload session and registered its items before a four-party
barrier released the uploads together. All originals went directly to Blob
Storage through upload SAS URLs, then through authenticated finalization and the
real Vision, clustering and Indexer consumers. This is an API/cloud-worker test,
not a browser UI or push-notification test.

| Actor | Destination | Source images | Filenames |
| --- | --- | --- | --- |
| A | Shared A library | CelebA 000001, 000002 | parallel-01.jpg, parallel-02.jpg |
| B | Shared A library | CelebA 000004, 000005 | parallel-03.jpg, parallel-04.jpg |
| C | Private C library | Same bytes as A | Same filenames as A |
| D | Private D library | Same bytes as B | Same filenames as A |

B deliberately held its final item unresolved until A finished, then held the
shared intake open for another 20 seconds. The shared wave remained `collecting`
with no Vision execution lease. C and D independently reached
`processing_pending` during that hold. Thus library-wide batching does not turn
into a global cross-library barrier.

An initial harness request incorrectly sent library switching to Core (404);
the frontend correctly routes it to Archive. The harness was corrected to match
that routing. Before any originals were uploaded in that attempt, A/C/D had each
registered a session. All six reserved items were explicitly cancelled through
the normal API. Those three cancelled sessions remain valid intake history in
the same collecting waves: expected session counts are three for shared A and
two each for private C/D, with actual processing members four/two/two. No stage
or completion record was forged to hide that setup mistake.

## Evidence and commands

Private credentials, upload events, wave observations and verification reports
are retained under ignored `.azure/wave-multiuser-20261024/`, with credentials
mode 0600. The operator scripts are bounded test drivers rather than production
features. Do not rerun `run.py` against existing session evidence; it rejects a
second upload invocation to avoid accidental duplicate tests.

```bash
.venv-photostore-tools/bin/python .azure/wave-multiuser-20261024/run.py
.venv-photostore-tools/bin/python .azure/wave-multiuser-20261024/verify.py
```

All three waves reached `complete` with `outcome=applied`, zero failed members.
Independent serving/isolation verification passed with the caveat below. Azure allocated a
Vision replica and reported a successful pull of its 1,846,805,035-byte image in
54.483 seconds, followed by container start at 07:27:16 UTC. This cold-start run
must not be compared directly with the previous warm 46.355-second canary.


## Filename-collision caveat

D's requested `parallel-01.jpg` / `parallel-02.jpg` names were returned as
`parallel-01-6968406f.jpg` / `parallel-02-54b6536e.jpg`. This matches the existing
`_resolve_filename_for_upload` cross-library differing-hash collision rule;
the driver follows the final filenames returned by the server.

The renamed destination metadata rows lack `upload_sha256_expected`,
`upload_sha256_actual` and `upload_sha256_match`. Code inspection explains why:
`finalize_uploaded_file` loads the new final-name row, while init's tracking
fields remain on the original-name row. It sets `fileHash` from the client hash
but `apply_upload_hash_result` returns immediately without an expected hash.
This is a real tracking/integrity-check-path gap, not a failed wave handoff. The
verification records it explicitly and additionally downloads all eight original
blobs through normal authenticated media access to compare their actual SHA-256
values against the local input bytes. It does not treat absent tracking fields
as verified tracking. Operator OAuth lacks direct Blob read permission; no new
role grants were added to work around that restriction.


## Verified result

The final verifier exited 0 with `passed=true`. The four upload intervals had a
common overlap of **3.152 seconds**, measured from each actor's first upload
start to its session finish; this proves concurrent clients rather than a
sequential replay. B's deliberately held second item made its interval longer.

| Library | Wave | Processing members | Positive faces / People | Publication obligations | Source attempts | Stage epochs |
| --- | --- | --- | --- | --- | --- | --- |
| A | `d960f06b5058486a9f12b4d0c9687b6a` | 4 | 4 / 4 | 8 | all 1 | Vision 1, cluster 1, index 1 |
| C | `b4189fd729344741aa95822a1930b255` | 2 | 2 / 2 | 8 | all 1 | Vision 1, cluster 1, index 1 |
| D | `a2d5f302e74b4752a27b3675e7567b0a` | 2 | 2 / 2 | 8 | all 1 | Vision 1, cluster 1, index 1 |

- Eight qualified immutable completion manifests agree with their source-bound
  face lookups and metadata face counts.
- Eight assignment decisions have eight matching applied markers. People IDs
  are disjoint across the three libraries, including identical input bytes in
  different libraries.
- Each wave's eight-obligation exact-ID coverage proof matches its required
  publication ID: 24 obligations across three completed waves.
- All eight originals were downloaded through normal authenticated media-token
  access and independently hashed against the local source files. All matched.
  Uploader attribution matches the actual account, including B while using A's
  library. The two missing renamed-upload hash-tracking records remain a finding.
- Authenticated gallery reads returned exactly four photos for both A and B,
  two for C and two for D, with no partial-index indication. Each user's People
  view matches that library's applied assignment IDs and face totals.
- Four unauthorized library switches returned 403. Three cross-library wave
  lookups returned 409 without exposing the target library/wave identifiers.
  C's lookup for shared-only `parallel-03.jpg` returned 404.
- All eight source attempts stayed at one; all nine execution leases succeeded
  at epoch one. No retry or forced completion was needed.

Wave-creation-to-completion times were 483.641 seconds (A), 459.987 (C) and
471.710 (D), including the cancelled setup attempt and cold starts. From the
last successful finalization response to durable completion, the corresponding
intervals were approximately 310, 314 and 326 seconds. These timings describe
this run; they are not throughput or scalability qualification.

No application code was changed during this test. The hash-tracking omission
needs a focused fix and regression test for renamed finalization; this run did
not corrupt stored bytes or strand a wave. Browser UI retention/push, album and
People CRUD, uploads spanning the 900-file client boundary, fault injection and
larger concurrent loads remain separate qualification work. General enrollment
stays disabled. Scale-to-zero after this run was not requalified; API checks
intentionally woke serving apps.


Subsequent state: the user later explicitly requested automatic enrollment. It
was enabled on the two library-creating apps and checked through real fresh
invitation acceptance; see [the enrollment activation audit](upload-wave-enrollment-2026-10-24.md).
The disabled-enrollment statements above describe the parallel test itself.
