# Latest recorded upload outcome — 2026-10-25

Read-only Azure Log Analytics inspection around 17:49–17:51 UTC. Workspace
`forenklaprep1-dev-law` was verified against current ARM configuration. No
deployment, recovery, queue, or library mutations were performed.

## Transfer summary

Latest browser batch: `6ea68f4f8d794d3eb73aa6062ac3e691`.

- Selected: **631 photos**, zero videos.
- Browser-reported newly uploaded: **602**.
- Duplicates: **19**.
- Browser-reported failed: **10**.
- Duration: **2,911,058 ms**, or 48 minutes 31 seconds.
- Browser event times: 16:25:08–17:13:39 UTC (17:25:08–18:13:39 Stockholm).

These are the latest batch end counters, not a fresh storage inventory. The
upload logs contain transient finalization errors and later successful
receipts; a filename anti-join did not identify ten permanently missing files.
Therefore the ten browser-reported failures must not be presented as ten
proven missing stored originals, nor attributed to one exception without
per-item confirmation.

## Browser preparation

Latest `upload_asset` event per batch/file/asset, excluding duplicate receipts:

| Asset | Accepted | Not submitted after browser preparation failed |
| --- | ---: | ---: |
| Preview | 541 | 61 |
| Thumbnail | 541 | 61 |
| Metadata packet | 565 | 37 |

For preview and thumbnail, the 61 cases divide into `unsupported_raw` (41),
`generation_failed` (16), and `resource_limit` (4). The 37 metadata packet
failures report `metadata_unsupported`. These are fallback requirements, not
necessarily failed original uploads or failed browser ExifTool extraction.
No generated/submitted derivative in this aggregation had a rejected backend
receipt. Nineteen additional per-asset receipts were duplicates.

`IMG_0128.HEIC` had accepted preview, thumbnail, and metadata receipts at
16:25:53 UTC. No HEIC skip was reproduced in this batch's receipt query.

## Wave progress and blocker

Recovery at approximately 17:45 UTC reported the wave opened around the start
of this batch, `e45117dec6b74bec8452bf22c6762bff`, in
`waiting_predecessor`, with predecessor `10922c76d5a343c4a3fe523abd764a25`.
That predecessor itself waits on `e6f6d0610479484caddb380f741df945`.

The older `e6f6d...` wave is `processing_pending`, with one exhausted dispatch
and no active stage lease at that observation. Recovery revived dispatch, but
the Vision attempt at 17:46:30 UTC failed again while qualifying
`dnxhd100_cid1260.mov`:

`ValueError: Skipped face work with existing faces requires an explicit prior receipt`

Trace: `wave_runtime.process_source` → `qualify_completed_processing` →
`ArtifactReceipts.capture`. The same exception was observed again around
17:50 UTC. This is an older wave's source-receipt failure blocking successors,
not evidence of a HEIC decoder failure. Latest-batch clustering/publication
completion was not demonstrated.

Upload logs also include `Conflict: Cleanup fenced upload source writes` and
batch-finalization retries during the transfer. Counts of those exceptions
are request attempts, not unique failed photos.
