# Library-wide photo backfill

Tools → Recovery → **Backfill all photos**, and Processing steps with the
entire-library scope, use `POST /api/admin/backfill/photos` on the admin API.
They force reprocessing of existing photos; videos and trashed rows are skipped.

## Bounded queueing

The endpoint previously scanned and enqueued the whole library synchronously.
Large libraries could exceed HTTP deadlines after already queueing some photos.
It now visits at most **10 metadata rows per request**, downloading narrow
11-row Table pages (the extra row establishes whether another batch exists).
It does not load the library into memory.

Request fields:

- `repair: true`, `confirm: "BACKFILL_ALL_PHOTOS"` are required.
- Optional `steps` scopes processing; omitted means all seven steps.
- Optional `continuation` is the exact last RowKey returned by the previous batch.

Response fields:

- `queued`, `skipped`, `failed`, `total`: counts for this batch, not the library.
- `complete`: stop when true; otherwise repeat with `continuation`.
- `steps`, `processingMode`: effective settings for the run.

Pagination uses a strict RowKey range inside the authenticated library partition.
It is not a snapshot: photos inserted before an already-visited key during the
run require another run. Errors while iterating storage pages return JSON with
partial counts. Failed queue sends are counted as failures, not successes or
video/deleted skips. An unavailable backend queue is rejected before any resets.

## Browser behavior

The frontend sends batches sequentially and shows cumulative progress. **Keep
the tab open until queueing finishes.** This is not a durable background
enumeration job, and interruption does not automatically resume after reload.
Successfully queued backend jobs remain scheduled even if a later batch fails.

After queueing, backend-only mode never loads browser AI or starts the browser
pipeline. Browser/both modes start browser processing; model unavailability is
reported separately from queueing success. Browser-only processing requires
keeping the tab open during processing too.

Deploy the **admin backend and frontend together**: older frontend builds assume
one response covers the entire library. API clients must now follow continuation
until `complete` is true. No infrastructure changes are required.