# Birthday album incident — 2026-10-22

Investigated read-only against forenklaprep1-dev-web/core/indexer and the deployment's Log Analytics workspace. The core app was Running on revision forenklaprep1-dev-core--0000007, backend image commit 4a408439ecb92d458db6959e4c8e64e9572dc4a1. The environment reported Succeeded and its apps Running. These current health checks do not establish historical health for every instant.

## Confirmed source state and requests

A read-only Table Storage lookup confirms the album created in this sequence is **Birthday**, with **16 inline member filenames**. Its createdAt is 2026-10-22T14:41:35.634851+00:00 and updatedAt is 14:41:35.775437+00:00. Stockholm was UTC+2; local time was **16:41:35**.

| Stockholm time | Evidence |
| --- | --- |
| 16:41:35.691 | POST /albums completed HTTP 200 in **66.8 ms**. |
| 16:41:35.799 | POST to the new album's /photos/add completed HTTP 200 in **55.4 ms**. |
| 16:41:35.859 | GET /api/albums/index completed HTTP 200 in 126 ms. It also queued library-ops work. |
| 16:41:44.386, telemetry receipt | The same browser session reported parsing an album index with **12 rows**; blob download was only 134 ms. |
| 16:41:54.158 | GET for Birthday's detail completed HTTP 200 in **18,312.2 ms**. Recorded I/O: six photometadata reads accounting for **16,946 ms** of I/O time. This request began approximately 16:41:35.846. |
| 16:42:04.381, telemetry receipt | The browser reported a later album-index parse with **13 rows**, downloaded in 25 ms, and a subsequent cached read with 13 rows. |

The client_span timestamps above are when the backend logged batches of browser telemetry, not exact render or publication timestamps. They establish observed older/newer index states within the sequence, not the precise second the album appeared on screen.

## Explanation and limits

The album and its membership were saved quickly. The user-visible wait occurred after the successful writes. Two mechanisms are evident:

1. The browser consumed an older 12-row album index before the later 13-row one. This is consistent with the newly created Birthday album being absent from the earlier snapshot. Logs record row counts, not every historical row ID, so they do not independently prove which row was missing or record the exact React list replacement.
2. Birthday's detail fetch took 18.3 seconds, predominantly in photo-metadata reads. Therefore the delay cannot be attributed solely to index rebuilding or album-creation latency.

The deployed code explains the UI's vulnerability: AskPage.saveAsAlbum calls createAlbum, starts addPhotosToAlbum without awaiting its completion, and navigates to Albums. createAlbum puts the returned album in React state but does not persist a creation receipt. loadAlbums later replaces the entire list with index-derived rows, applying only existing field overlays. There is no pending album-creation/membership overlay to preserve the new album while an older index is read. addPhotosToAlbum also calls openAlbum after saving, which accounts for the observed immediate detail request. The detail request's successful return does not by itself guarantee the album header remains in the concurrently refreshed list.

Accordingly, the earlier assertion that this incident was definitely only a stale-index overwrite was too strong. The logs support index lag plus a separately confirmed slow detail read. Historical pixel-level UI state and the exact overwrite instant were not captured. No application changes, deployments, repairs, or user actions were performed during diagnosis.

## Relevant log evidence

Times in these excerpts are UTC. Other sessions and public-share URLs/tokens are intentionally omitted.

```text
2026-10-22 14:41:35,799 INFO perf PERF event=request method=POST path=/albums/af9c416e-a055-42f0-a63b-f37598d6aad3/photos/add status=200 ms=55.4 rss_mb=592 rss_delta_mb=0 inflight=1 bytes=671 rid=7jwe-2j view=ask sess=7jwees51 io_calls=5 io_ms=51 io_mb=0.00 spans=- io_top=table:PATCH:albumsdirtyset(..):x1/13ms/0KB,table:GET:photousers(..):x1/12ms/0KB,table:PATCH:photoalbums(..):x1/10ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB,blob:GET:lexical-index:x1/7ms/0KB
2026-10-22 14:41:35,691 INFO perf PERF event=request method=POST path=/albums status=200 ms=66.8 rss_mb=335 rss_delta_mb=0 inflight=1 bytes=218 rid=7jwe-2i view=ask sess=7jwees51 io_calls=4 io_ms=42 io_mb=0.00 spans=- io_top=blob:PUT:lexical-index:x1/13ms/0KB,table:PATCH:photoalbums(..):x1/11ms/0KB,table:PATCH:albumsdirtyset(..):x1/9ms/0KB,table:GET:photousers(..):x1/9ms/0KB
2026-10-22 14:41:35,859 INFO perf PERF event=request method=GET path=/api/albums/index status=200 ms=126.0 rss_mb=335 rss_delta_mb=0 inflight=1 bytes=615 rid=7jwe-2k view=ask sess=7jwees51 io_calls=8 io_ms=87 io_mb=0.00 spans=index.share.read:4ms,index.share.read:0ms,index.share.read:0ms io_top=queue:POST:photostore-library-ops:x1/41ms/0KB,blob:GET:lexical-index:x3/17ms/2KB,table:GET:photojobs(..):x2/15ms/0KB,table:PATCH:photojobs(..):x1/8ms/0KB,table:GET:photousers(..):x1/6ms/0KB
2026-10-22 14:41:44,386 INFO perf PERF event=client_span user=owner sess=7jwees51 name=index.albums.parse ms=0 view=albums rows=12
2026-10-22 14:41:47,052 INFO perf PERF event=request method=GET path=/api/albums/index status=200 ms=127.5 rss_mb=335 rss_delta_mb=0 inflight=1 bytes=615 rid=7jwe-2q view=albums sess=7jwees51 io_calls=3 io_ms=26 io_mb=0.00 spans=index.share.read:9ms,index.share.read:4ms io_top=blob:GET:lexical-index:x2/17ms/1KB,table:GET:photousers(..):x1/9ms/0KB
2026-10-22 14:41:54,158 WARNING perf PERF event=request method=GET path=/albums/af9c416e-a055-42f0-a63b-f37598d6aad3 status=200 ms=18312.2 rss_mb=597 rss_delta_mb=4 inflight=1 bytes=15244 rid=7jwe-2l view=albums sess=7jwees51 io_calls=8 io_ms=16961 io_mb=0.00 spans=- io_top=table:GET:photometadata(..):x6/16946ms/0KB,table:GET:photoalbums(..):x1/8ms/0KB,table:GET:photousers(..):x1/7ms/0KB
2026-10-22 14:41:58,027 INFO perf PERF event=request method=GET path=/api/albums/index status=200 ms=35.5 rss_mb=335 rss_delta_mb=0 inflight=1 bytes=615 rid=7jwe-2w view=albums sess=7jwees51 io_calls=2 io_ms=14 io_mb=0.00 spans=index.share.read:5ms io_top=table:GET:photousers(..):x1/7ms/0KB,blob:GET:lexical-index:x1/7ms/0KB
2026-10-22 14:42:04,381 INFO perf PERF event=client_span user=owner sess=7jwees51 name=index.albums.parse ms=0 view=albums rows=13
```
