# Follow-up HAR: stale browser catalog — October 28, 2026

The replacement `download.devtools` capture spans 09:51:44–09:52:45 UTC (10:51:44–10:52:45 Stockholm).

## Evidence

- 3,870 thumbnail `404` responses represent 2,097 distinct paths, with repeated requests. All 2,097 match reservation-only entries in the captured catalog; their upload dates and processing results are absent.
- Another 301 entries have status 0, rather than a recorded HTTP failure. There are no captured HTTP 403 or 5xx responses.
- The light index-build job is running initially and reports completion at 09:51:54 UTC.
- The first sort-index API response reports the old version. The second reports the new published version `2026-10-28T09:51:30.518759+00:00`.
- The blob request at 09:51:57 UTC is marked `_fromCache: disk`. Its `Last-Modified` is October 27, 21:38:53 UTC. Its embedded catalog is still v4 with 13,749 rows and 3,265 reservation-like entries, the same source version as the earlier HAR.
- A direct network read of the exact latest signed URL from the supplied capture returns the actual published blob: v5, 10,484 rows, zero reservation-like entries, source version `2026-10-28T09:51:30.518759+00:00`, and `Last-Modified` October 28, 09:51:53 UTC.

The backend reservation exclusion and catalog rebuild succeeded. The remaining burst in this capture comes from an old browser HTTP-cache body served for an overwritten, stable blob URL. This does not establish whether the three older finalized thumbnail failures are repaired; this capture's confirmed failures are all reservation entries.

## Local correction

Sort, album, and people catalog downloads now request `cache: 'no-store'`. Their version-aware IndexedDB caches continue to provide normal reuse. Before reusing a saved index, the client also checks that its recorded payload version agrees with the manifest version when a payload version is available. This repairs local copies an older client saved under the new manifest label after receiving an old HTTP-cached body.

Tests model a stale HTTP cache and a poisoned IndexedDB entry for all three catalog kinds, and verify that a repaired matching snapshot is reused without another blob download. Existing gallery deletion/restore fixtures now use matching manifest and payload versions for the unchanged-catalog case.

No deployment, remote mutation, or push was performed. The signed URLs and tokens remain outside this report.

Validation: 32 focused frontend tests passed, TypeScript typechecking passed, and `git diff --check` passed.
