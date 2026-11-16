# Dedicated Download library

Status: the Chrome/Edge folder download is implemented (2026-11-16): `backend/library_download.py`,
`frontend/src/services/libraryDownload/`, and the control in Library settings. The zip fallback for other
browsers is not built yet. Companion to `docs/library-clean-dedicated.md`.

As built, differing from the text below: large files are fetched as 64 MB ranges in sequence on one connection
and joined (large files go first so the tail is small files); a file is done when it exists with the manifest's
size, since files are written atomically, so there are no `.part` files; a hash mismatch is fetched once more
and the file is kept as "unverified" when storage returns the same bytes twice; the decisions at the end were
all taken as proposed.

Download library is a direct admin action with strict resiliency and speed requirements, so it gets its own
code: one backend module for the manifest (`backend/library_download.py`) and one frontend module for the
transfer (`frontend/src/services/libraryDownload/`). It does not reuse the photo viewer's media URLs, the
upload machinery or the cluster worker.

## Where things stand

- **There is no Download library button.** The browser downloader (`libraryExportDownloader.ts`,
  `opfsDownloadStaging.ts`, `serviceWorkerDownload.ts`) was deleted with the legacy UI on 2026-11-05 (c500018).
  `frontend/public/download-sw.js` and the client's manifest call are orphans.
- **Two backends survive.** `GET /api/library/export/manifest` (archive role: 500 `{filename, blobName}` per
  page plus one container read SAS) and the server zip worker `_execute_library_download` (cluster role:
  2 GB `ZIP_STORED` parts written to `library-exports`, 8 concurrent blob reads).
- **Measured** (laptop in Sweden to East US storage, 2026-11-15): one connection 8.1 Mbit/s, four or more
  9.9 Mbit/s on a ~10 Mbit/s line; ~92 ms round trip, ~0.4 s for a first HTTPS request.

## What decides the time

At 10 Mbit/s, 150 GB takes 33.3 hours at the theoretical maximum. No design changes that. What a design
controls:

| Loss | Old browser downloader | Old server zip | This design |
|---|---|---|---|
| Unused bandwidth (one stream, gaps between files) | 4 streams, but each file was staged to browser storage, then saved again | 1 stream per 2 GB part | 6 streams straight to disk; small files overlap their round trips |
| Restart cost after sleep, network drop, closed tab | file-level resume, but every save was a browser download (thousands of prompts/entries) | a failed part restarts from 0: up to 2 GB (27 min at 10 Mbit/s) | byte-level resume inside a file, file-level skip from the folder itself |
| Waiting before the first byte | none | the whole library is zipped first (~1 h at 150 GB, cluster 2 vCPU awake) | none |
| Extra copies | 2× local disk while staging | 150 GB temporary copy in storage | none |
| Token expiry on a 34 h run | not handled | not handled | SAS renewed before expiry |
| Apps awake | archive, paged throughout (~9 h) | cluster ~1 h | archive ~2 short wakes |

So the target is: **total time = bytes / line rate + a few minutes, however many interruptions happen.**

## Design

### Transfer: browser, straight from Blob Storage into a folder

On Chrome and Edge desktop (File System Access API, `showDirectoryPicker`):

1. The user picks a destination folder once. The handle is kept in IndexedDB, so a later visit resumes into
   the same folder after one permission click.
2. The page fetches the **whole manifest once** (below), writes it to the folder as
   `.keepsake-download/manifest.json`, and from then on needs the app only to renew the SAS.
3. A scheduler keeps **6 connections** busy (the browser's per-host limit over HTTP/1.1; measured throughput
   was already flat from 4 streams up on a 10 Mbit/s line):
   - files are taken largest-first within a sliding window so one huge video does not leave five connections
     idle at the tail, and small files fill the gaps;
   - a file over 64 MB is split into ranges only when fewer than 6 files remain, so the tail still uses the
     whole line.
4. Each file streams `fetch(url).body` into `name.part` through `createWritable()`, then is renamed to its real
   name. Nothing is staged in browser storage; memory stays at a few chunks per connection.
5. **Resume is derived from the folder, not from a database.** On start (or restart), a file whose final name
   exists with the manifest's size is done; a `.part` file continues with `Range: bytes=<its size>-`; anything
   else starts fresh. Originals are immutable (each upload gets its own blob name), and the size and hash check
   in step 6 catches the rare changed blob. Deleting the folder state is always safe.
6. **Verification.** Size always. Content hash when the manifest has one (`fileHash`, SHA-256 from upload):
   hashed incrementally while streaming, so it costs no second read; a mismatch re-downloads that file once
   and then reports it.
7. **Failures.** Per-file retry with backoff (network errors, 5xx, 503 throttling); 403 means the SAS expired:
   renew and continue; `navigator.onLine`/fetch failures pause all workers and probe every 10 s; a file that
   fails 5 times is listed in `.keepsake-download/failed.json` and the run continues. A Screen Wake Lock is
   held while running.
8. **Layout.** `YYYY/MM/<original filename>` from capture date (falling back to upload date), Live Photo clips
   next to their stills, name collisions suffixed `-2`. A `library.json` with albums, people names, captions
   and favourites goes at the top so the download is a usable archive, not only files.
9. **Progress.** Bytes and files done, current rate, ETA from the last 60 s; state survives reload because it is
   recomputed from the folder.

### Fallback for Safari, Firefox and phones

No folder access there. Two options, in order of preference:

- **Streamed zip built in the browser:** the same scheduler feeds a streaming `ZIP_STORED` writer whose output
  is served by `download-sw.js` as one ordinary download (`Keepsake-<library>-part-N.zip`, 4 GB parts). One
  save prompt per part, no staging, full line rate. A part cannot resume mid-way, but a finished part is never
  repeated.
- **Server zip parts** (`_execute_library_download`) stay as the last resort for browsers without service
  worker streaming, unchanged.

### Manifest: one dedicated endpoint, whole library, self-sufficient

`GET /api/library/download/manifest` on the archive role, in `backend/library_download.py`:

- streams the whole library as NDJSON (one line per file) from a single projected partition scan
  (`RowKey, anonymousImageId, size, fileHash, uploadDate, captureDate, contentType, livePhotoOf,
  processing_state`), so 50,000 files are ~50 table pages and ~6 MB gzipped: one request, a few seconds, one
  short archive wake instead of 100 paged calls spread over the whole run;
- takes the blob name from the row's own `anonymousImageId` (no per-file name-mapping lookups);
- includes `size` and `fileHash`, and total bytes/files in a trailer line, so the client knows the total
  before the first byte and can verify every file (which of these fields every metadata row really carries is
  to be confirmed against live rows; a row without a size falls back to the blob's `Content-Length`);
- `GET /api/library/download/token` returns a fresh container read SAS (user-delegation, capped at the
  delegation key's lifetime, 24–48 h); the client calls it at start and again 30 min before expiry or on a
  403. This is the only server call during the transfer.

Both require the library owner, as the current manifest does. The old `/api/library/export/manifest` and
`/api/library/download/*` job endpoints stay until the UI ships, then go.

### Security notes

- The SAS is container-scoped read, as today: it can read every library's originals in `images` for its
  lifetime. This design does not make that worse, but it holds the SAS for the whole run. Options, by cost:
  keep as is; shorten to 2 h and renew (more wakes); or issue per-blob SAS in batches (no cross-library
  exposure, more signing). See decision 4.
- Storage CORS already allows `GET`/`HEAD` with all headers from the web origin, so `Range` requests need no
  deployment change.

## Expected numbers (50,000 files, 150 GB, 10 Mbit/s)

| | Old server zip | This design |
|---|---|---|
| Before first byte | ~1 h zipping (estimate) | seconds |
| Transfer | 34–41 h | ~33.7 h (measured 9.9 Mbit/s with parallel streams) |
| Cost of one interruption | up to 27 min | under a minute |
| Apps awake | cluster ~1 h | archive ~10 min in total |
| Extra storage | 150 GB temporary | none |

On a faster line the same design scales with it (100 Mbit/s: ~3.4 h) until disk or the 6-connection limit
binds; range-splitting large files is the lever beyond that.

## Tests and benchmarks

- **Backend:** manifest streams in bounded memory at 50k rows (Azurite), skips deleted rows, pairs Live Photo
  clips, owner-only; token endpoint caps expiry at the delegation key's.
- **Frontend unit:** scheduler ordering and connection cap; resume decisions from a fake folder (done / partial
  / stale etag / wrong size); retry and pause logic; zip writer against a reference unzip.
- **Emulation:** headless Chrome with the real page against Azurite through a throttling proxy (10 Mbit/s,
  90 ms, injected drops and a mid-run SAS expiry), 2,000 files with the production size mix; assert every byte
  matches and the run finishes within 3% of bytes / line rate.
- **Real:** one full run on a live environment when there is one.

## Decisions needed

1. **Where the button lives.** Proposed: Library settings (next to Clean library), owner only.
2. **Folder layout.** Proposed `YYYY/MM/filename` by capture date. Alternative: flat, original names only.
3. **Include `library.json`** (albums, people, captions, favourites)? Proposed: yes.
4. **SAS scope:** keep the container-wide read SAS (today's behaviour), or move to per-blob SAS batches?
   Proposed: keep for the first version, shorten to 12 h with renewal.
5. **Fallback order** for Safari/Firefox/phones: in-browser streamed zip first, server zip last. Proposed: yes;
   build the Chrome/Edge folder path first and ship the fallback second.
