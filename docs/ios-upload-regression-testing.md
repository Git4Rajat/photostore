# iOS browser upload regression investigation

## Change comparison

The successful iPhone/iPad tests establish that the deployed version works for
those selections. They are not a controlled A/B replay of the earlier failure.
Runtime changes in commit `75025bf` were:

| Change | What it can affect | Evidence / limits |
| --- | --- | --- |
| End-to-end 3-second deadline around the library-hash lookup | Releases every upload lane from its shared startup wait, including a stalled auth interceptor | Confirmed by live timeout spans and initialization before the server response; also reproduced by a never-settling-request test |
| Single HTTP attempt and removal of the outer retry loop | Stops optional dedup from repeatedly retrying a cold/unreachable backend | Current request telemetry shows attempt 0; no failing old iOS retry trace was recovered |
| Request timeout and abort signal | Cancels the download; also opts this GET out of shared in-flight request coalescing | Confirmed cancellation; avoiding an older shared stalled GET is plausible but not demonstrated |
| Picker-return count/byte/timing span | Observability only; selection handling is unchanged | Records 1,669 iPhone and 386 iPad files; cannot itself raise a picker limit |
| Native `cancel` listener | Stops warm-up after cancellation and gives neutral recovery feedback after a long picker wait | Could help recovery after cancellation; no cancel span in the successful sessions examined |
| Tests and investigation document | No shipped runtime behavior | Validate synthetic selection, queue/resume and timeout paths, not native acquisition |

No picker replacement, larger file-count cap, format conversion, SHA-256 change,
chunking change, or resume-matching change was part of that fix. Deployment/reload
can reset in-memory tab state and is an uncontrolled confounder, not a proven fix.
The wait removal is the demonstrated startup improvement; the precise historical
300-selected/100-started failure remains unproven.

## Prebuilt library-wide dedup

The follow-up implementation removes the upload's hash-download wait entirely
and moves the library-wide hash-table scan off the HTTP path:

- A dedicated `index_build` job with scope `dedup` streams one library's hash
  table into an immutable gzip JSON blob in the existing private `lexical-index`
  container. It does not build gallery, search, or people indexes.
- `/upload/known-hashes` still requires library authorization. It reads a small
  snapshot pointer/deletion version and serves the compressed file unchanged;
  no table scan or full-response serialization occurs inside the request.
- Missing snapshots queue a coalesced build and return `available: false`
  immediately. Uploads do not await this download. Older snapshots can be
  served while an additions-only refresh is queued after 60 seconds; light/full
  index maintenance also queues a dedup-only prebuild, with independent retries
  so snapshot failures cannot fail gallery indexing. There is no recurring scan
  while the library is idle.
- Hash removal and its deletion-version update share one Table transaction.
  Snapshots built before a known deletion are not served. Deletes during build
  or download are checked too; storage/version-read failures fail closed.
- The browser starts the optional fetch when opening the picker, without
  awaiting it or losing the original user gesture, and reuses a successful
  recent prefetch. An empty/unavailable prefetch gets another chance when
  selection returns. Superseded/cancelled fetches cannot replace a newer list.
  Downloaded library hashes are replaced on refresh rather
  than retained forever; hashes learned by uploading in this tab stay separate.
- Download failure, timeout, or an unavailable snapshot gets up to three total
  attempts, with 2- and 5-second retry delays and a 60-second end-to-end limit
  per attempt. These limits bound background work only: there is no 3-second
  gate or wait before spawning upload workers. A download arriving during an
  upload immediately becomes usable by files that have not started transferring.
  Concurrent callers within the provider share the same background download;
  short internal batches reuse a downloaded nonempty list for up to 60 seconds.
  Closing the picker during an ongoing upload does not cancel that download.
  A confirmed same-tab deletion cancels older pending downloads so a delayed
  pre-deletion response cannot put the removed hash back into the browser map.
- Backend duplicate checking remains authoritative. Recently added hashes can
  be absent until the queued refresh finishes; files started before the list
  arrives, or after exhausted download retries, may transfer duplicate bytes
  and fall back to backend detection. This is not a guarantee of pre-transfer
  detection for every newly added file on another device.

### When hashes become available

The authoritative hash table and the prebuilt download are separate:

- Each successfully finalized photo writes its hash to the server table. It
  does not wait for the entire upload to finish. The uploading tab also keeps
  its own hash claims, so same-tab duplicates need no snapshot refresh.
- The downloadable gzip file is rebuilt from that table, not appended once
  per photo. A missing/deletion-invalidated file queues a rebuild on request.
- A request for a snapshot at least 60 seconds old serves it (if its deletion
  version still matches) and queues an additions refresh. This is an age check
  on requests, not a timer rebuilding every minute while idle.
- Completion of normal light/full index maintenance queues a dedup-only build.
  Existing ipworker triggers request light maintenance at queue drain and at
  `IPWORKER_INDEX_REBUILD_MILESTONE` (default 10,000 processed photos), subject
  to existing trigger cooldown/coalescing.
- The browser requests a dedup-only refresh on successful completion, failed/
  paused completion, Stop, or discard. It waits for pending file-finalize calls
  to settle before sending this request, including on Stop/error; the refresh
  request and build do not block UI cleanup. Normal internal batches defer the
  explicit request until the final queued batch ends, avoiding a scan per batch.
- Successful single/batched finalize requests also queue one coalesced,
  delayed `dedup-idle` check. Using the existing upload-activity heartbeat, it
  reschedules while uploads are active and rebuilds after the configured quiet
  window (default five minutes). This survives a tab crash/force-close where
  browser exit callbacks cannot be relied on. It is a queued check for libraries
  with recent successful uploads, not a fleet-wide polling loop.
  Checks are five minutes apart by default, so abrupt exits can take about
  5-10 minutes of inactivity plus queue/build time; explicit end requests do
  not wait for that quiet window.
- The delayed check skips its rebuild if a snapshot scan started after the
  latest upload activity and its deletion version still matches. A snapshot's
  publication time alone does not prove it included uploads arriving mid-scan.
- New hashes reach other browsers only when their downloaded snapshot includes
  them. An already downloaded list is not continuously pushed/appended during
  an ongoing upload; recent cross-device uploads can still miss early skipping.

Build/read spans and `upload_hash_snapshot_built` record row count and compressed
bytes for canary comparison. Synthetic compression tests do not certify mobile
download latency. No environment or deployment change is included in this work.

### Local validation

A 100,000-entry synthetic library (SHA-256 keys and `IMG_000000.HEIC`-style
filenames) produced 8,500,012 bytes of JSON and a 4,274,847-byte gzip snapshot:
49.7% fewer download bytes. The builder scanned the table once; reading the
snapshot added no scan. Storage was an in-memory test double, so its local
build/read timings are not estimates of Azure or mobile performance. Real
filenames and network conditions will change the size and download duration.

Verification before the background-download adjustment: 90 snapshot/dedup/
index-maintenance tests and 83 upload/deletion/role-regression tests passed,
as did all 140 frontend tests, TypeScript checking and the production build.
On this machine's Node 26, the frontend suite needs
`NODE_OPTIONS=--no-experimental-webstorage` to avoid a pre-existing conflict
between Node's experimental storage globals and the browser test environment.

After the background-download adjustment, all 146 frontend tests, TypeScript
checking and the production build passed. The upload test starts transferring
before the hash list arrives, keeps the download alive beyond three seconds,
and skips remaining duplicates in that same upload after a ten-second download.
Additional cases cover bounded retries, unavailable snapshots, cancellation,
old responses, deletion and sharing one pending download across upload runs.

Upload-end refresh tests cover completion/failure/Stop, pending finalization,
authorized reason validation, queue coalescing/unavailability, successful
single/batched finalize scheduling, quiet-window rescheduling, and skipping
redundant builds after an explicit refresh. Builds still depend on functioning
storage/queue services; neither browser callbacks nor queues promise immediate
completion while the device is offline or Azure services are unavailable.
Final upload-end verification: 234 selected backend tests and all 156 frontend
tests passed, along with TypeScript checking, the production build and
`git diff --check`. The Stop case also verifies that a late finalization is
settled before requesting the snapshot refresh.

## Historical baseline

The local Git reflog retains the pre-squash July source. July 21 commit
`127ab0d` and July 26 commit `5cf7fab` both used the native HTML file input,
full-file SHA-256 hashing, and direct Azure Blob uploads. July 26 has a
successful image publication run:
https://github.com/Git4Rajat/photostore/actions/runs/30216898744

These are source/build records, not proof of a particular 3,000-file upload.
No June source/build records were recovered in the investigation, and the
available LAW workspace returned no June/July console records (its current
retention is 30 days).

Later changes include the August 21 known-hash prefetch, August 22 internal
900-file session splitting, and subsequent resume/queue state fixes. Do not
roll back backend authorization or integrity changes to test an older frontend.

## Reproduced behavior and narrow change

Component tests drive the current file input with 300 and 3,000 distinct files
and mock only external storage/API/processing boundaries. All selected files
reach initialization and finalization; the 3,000-file case drains all four
internal sessions. A paused 100-file session followed by a 300-file reselection
also resumes the original 100 and uploads the additional 200.

A separate test failed before the change: a known-hash request that never
settles prevents every transfer from starting. The optional prefetch now has a
3-second end-to-end deadline, a single HTTP attempt, and cancellation. Late
responses are ignored. Backend deduplication and original SHA-256 reporting
remain unchanged. This does not establish the cause of native picker failures
or the reported 100-file selection issue.

## Live follow-up: October 11

Read-only LAW investigation found two sessions running the changed frontend:
`bzon8wt9` (iPhone Safari) and `obskztae` (iPad Chrome). The picker spans returned
791 and 1,669 files in the iPhone session, and 50 and 386 in the iPad session.
These are actual browser-return counts, not inferred upload totals.

All six recorded hash-list client requests timed out at approximately 3,003-
3,011 ms with status 0 and zero received bytes. Five matching backend requests
completed with HTTP 200 in 5,378-7,123 ms, returning 6.23-6.29 MB JSON responses.
Each scanned 77-78 hash-table pages. One client request had no matching completed
backend request in the query window. Backend completion does not mean the
already-aborted browser accepted or cached its response.

The first iPhone initialization completed at 10:26:34.014 UTC; its matching
hash-list request completed at 10:26:37.783 UTC (`rid=bzon-e`). Initialization
therefore proceeded before the hash-list response completed. The unchanged
worker gate (`await knownHashesPromise`) previously required that response, or
exhaustion of its retry chain, before starting any file. Older October 8-9 client
requests took 23.7-27.8 seconds even when server processing was under 0.4 seconds;
the logs do not identify why that client/server timing gap occurred.

Correlating backend timestamps with access-log user agents identifies those
23.7-27.8-second October 8-9 requests as desktop Edge, not iOS. Desktop Edge
also completed October 10 hash-list requests in 4.7 and 5.9 seconds (HTTP 200,
2.93 and 3.12 MB). Thus the old dependency delayed desktop uploads too, but
eventually resolved. This is not a controlled same-library/device comparison:
the later mobile responses were approximately twice as large, and there is no
pre-change failing mobile trace with a recorded picker count. A shared wait
cannot by itself establish a device-specific selection-count failure. Do not
attribute the historical 100-file behavior to memory, native acquisition, or
this lookup without further evidence.

Duplicate detection still has two paths without a fresh library list: the
browser's in-memory hashes from uploads in the same mounted provider, and the
backend's finalize-time check. The displayed `skippedDuplicateCount` combines
both; it is not evidence that a full library list arrived before transferring.
Focused component tests cover a same-tab reselection with both lookups timing
out (no second transfer) and a backend duplicate (transfer followed by the same
displayed skipped-duplicate count).

This establishes a real startup dependency that the change now bypasses. It
does not establish that iOS previously returned only 100 files, or explain the
entire reported 300-to-100 failure. No picker, selection cap, resume matching,
SHA-256, or transfer behavior was changed by commit `75025bf` beyond bounding
this lookup; the other runtime changes record picker events and stop warm-up
on native cancellation. No deployment or reprocessing was done in this follow-up.

## Real-device test

Keep the same iPhone/iPad, iOS version, browser version, library, and selected
assets for comparisons. Test 100, 300, and 3,000 items, separately for locally
available originals and iCloud-only originals. Also test a broader reselection
against a paused upload. Do not substitute a compatibility/re-encoding path.

Capture the browser return count before deduplication and resume matching:

- `upload.picker.change`: `rows` is the exact returned file count; `bytes` is
  the selected source-byte total; `ms` is time since opening the picker.
- `upload.picker.cancel`: the picker returned no new selection. This does not
  distinguish a deliberate cancel from a native acquisition failure.
- `upload.known_hashes.timeout`: the optional lookup hit its startup budget.

These use the existing client performance collector and `/api/perf/client`
allowlisted span fields. No filenames, file contents, hashes, or SAS URLs are
included in these spans. Telemetry is best-effort and can be disabled with the
existing performance switch. A browser that never emits either picker event
is still not directly observable from these spans.

After the changed frontend is deployed, inspect LAW:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h)
| where Log_s has "PERF event=client_span" and Log_s contains "name=upload."
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated asc
```

If the picker returns 100 from a 300-item selection, investigate native/provider
acquisition. If it returns 300, compare initialized/finalized/failed/duplicate
counts across every session. Synthetic component tests cannot validate the
real iOS Photos/Files picker, iCloud fetching, or original representation chosen
by the OS before files reach JavaScript.
