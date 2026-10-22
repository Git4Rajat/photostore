# Red-dress album delete/create/reload incident — 2026-10-22

Read-only investigation of forenklaprep1-dev using Log Analytics, the owner album partition, and its index-build job records. No album was modified, deleted, restored, or recreated by this investigation.

## Confirmed sequence

All times below are Stockholm time (UTC+2).

- **16:50:23 and 16:50:26:** two delete-multiple requests returned HTTP 200 and wrote album rows.
- **16:50:34 and 16:50:35:** two permanent purge requests returned HTTP 200 and issued album-row DELETEs, for IDs ef41f7df-374d-42bc-9886-bb32c098570d and 34f4ebe4-71af-45d0-8960-21f61bed2454. Those records are absent from the current source partition. This explains their absence from Recently Deleted after purge.
- **16:51:14:** a new **dressed in red** album was created: 32a5d650-52f4-425a-98d8-28447640abc8. The source currently stores **273 photos**, with no deletion flag. This is a different record from either purged album.
- **16:51:39 and 16:51:42:** two further delete-multiple requests returned HTTP 200, but each recorded only the user lookup and a failing album lookup, with no album write. The route returns HTTP 200 even for per-album failures. The request logs do not include the submitted album IDs or response bodies, so the exact target IDs cannot be independently recovered from these entries.
- **16:51:46.665:** the latest observed albums index-build job completed. Its current row still has this timestamp; there is no newer completion in that job record as of inspection.
- **16:52:00:** a new **red dress** album was created: 21e40bc1-764b-4fcf-a399-374cb49ae19b. The source currently stores **83 photos**, with no deletion flag. Create completed in 33.6 ms and add-photos in 66.3 ms.
- **16:52 onward:** new browser sessions after reload read/reused an album index with **12 rows**. Telemetry still reported 12 cached rows at **16:56:01**, whereas the source partition contains **13 album records**, none marked deleted.
- **16:52:57 and 16:53:01:** trash requests succeeded and returned small empty-list responses; the current source also contains no trashed albums.

Client telemetry log times reflect batched receipt, not precise render times. The same limitation applies as in the Birthday investigation.

## Interpretation

The new red dress album is saved in backend storage; this is not evidence of a lost create operation. The UI is behind the source. The surviving dressed in red album is newly created after the purge calls, not a resurrection of either purged source record.

Three implementation gaps explain the observed behavior:

1. **False delete success:** StoreProvider.deleteAlbum/deleteAlbums removes rows optimistically and treats any resolved HTTP request as success. It does not inspect success, deletedAlbums, or albumErrors from delete-multiple. That endpoint deliberately returns HTTP 200 with success=false on an album lookup failure. The two later failing lookups therefore have no deletion write, yet the UI can announce success. The still-active dressed in red source record was not successfully deleted in the observed sequence.
2. **No durable creation/deletion overlay:** createAlbum and album deletion only change React state and invalidate a cache. They do not persist a pending creation or tombstone. Older index rows can replace or resurrect local UI state after a fetch/reload. Trash is an authoritative source read and therefore need not agree with a stale index-based active-album view.
3. **Index scheduling/freshness gap:** source contains 13 records but repeated browser reads see the 12-row index. The latest album-build job predates creation of red dress. This is more persistent than the short rebuild delay seen for Birthday. Code inspection identifies a plausible cause: touch_user_albums_index_state skips manifest writes after a process-local dirty flag is set; only rebuilding clears that flag, and the rebuild may occur in a different process. _save_album_entity writes the per-album dirty set but does not itself enqueue a durable rebuild. This is a code-supported hypothesis for the missed new build, not direct observation of the live process-local flag.

The current Azure identity could read Table records and diagnostic logs, but raw album manifest/body blob reads were denied by Azure RBAC. No roles, credentials, or authentication method were changed to bypass that denial. Therefore the raw published snapshot and its dirty flag were not independently inspected; the source/index mismatch is established by source records and browser row-count telemetry, with the exact missing snapshot row inferred from the sequence.

## Next implementation work

Validate delete response bodies and roll back only failed IDs; persist album creation and deletion receipts/tombstones across navigation and reload; reconcile active and trash projections against confirmed source state; and make album rebuild scheduling durable so every successful mutation is eventually indexed even when an earlier worker has cleared the shared manifest.

## Relevant evidence

These original log excerpts use UTC. Public-share routes and tokens are excluded.

```text
2026-10-22 14:50:23,425 INFO perf PERF event=request method=POST path=/albums/delete-multiple status=200 ms=104.9 rss_mb=380 rss_delta_mb=1 inflight=1 bytes=149 rid=7jwe-3y view=albums sess=7jwees51 io_calls=5 io_ms=94 io_mb=0.00 spans=- io_top=blob:PUT:lexical-index:x1/45ms/0KB,table:GET:photousers(..):x1/15ms/0KB,table:PATCH:albumsdirtyset(..):x1/13ms/0KB,table:GET:photoalbums(..):x1/13ms/0KB,table:PATCH:photoalbums(..):x1/10ms/0KB
2026-10-22 14:50:26,994 INFO perf PERF event=request method=POST path=/albums/delete-multiple status=200 ms=39.0 rss_mb=380 rss_delta_mb=0 inflight=1 bytes=149 rid=7jwe-3z view=albums sess=7jwees51 io_calls=4 io_ms=36 io_mb=0.00 spans=- io_top=table:GET:photoalbums(..):x1/10ms/0KB,table:PATCH:photoalbums(..):x1/9ms/0KB,table:PATCH:albumsdirtyset(..):x1/9ms/0KB,table:GET:photousers(..):x1/8ms/0KB
2026-10-22 14:50:34,007 INFO perf PERF event=request method=POST path=/albums/ef41f7df-374d-42bc-9886-bb32c098570d/purge status=200 ms=44.4 rss_mb=380 rss_delta_mb=0 inflight=2 bytes=17 rid=7jwe-42 view=trash sess=7jwees51 io_calls=4 io_ms=42 io_mb=0.00 spans=- io_top=table:PATCH:albumsdirtyset(..):x1/14ms/0KB,table:DELETE:photoalbums(..):x1/12ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB,table:GET:photousers(..):x1/7ms/0KB
2026-10-22 14:50:35,311 INFO perf PERF event=request method=POST path=/albums/34f4ebe4-71af-45d0-8960-21f61bed2454/purge status=200 ms=43.7 rss_mb=380 rss_delta_mb=0 inflight=1 bytes=17 rid=7jwe-43 view=trash sess=7jwees51 io_calls=4 io_ms=41 io_mb=0.00 spans=- io_top=table:DELETE:photoalbums(..):x1/12ms/0KB,table:GET:photousers(..):x1/10ms/0KB,table:PATCH:albumsdirtyset(..):x1/10ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB
2026-10-22 14:51:14,141 INFO perf PERF event=request method=POST path=/albums status=200 ms=37.7 rss_mb=625 rss_delta_mb=0 inflight=1 bytes=224 rid=7jwe-49 view=ask sess=7jwees51 io_calls=3 io_ms=35 io_mb=0.00 spans=- io_top=table:PATCH:photoalbums(..):x1/12ms/0KB,table:PATCH:albumsdirtyset(..):x1/12ms/0KB,table:GET:photousers(..):x1/11ms/0KB
2026-10-22 14:51:14,251 INFO perf PERF event=request method=POST path=/albums/32a5d650-52f4-425a-98d8-28447640abc8/photos/add status=200 ms=52.1 rss_mb=625 rss_delta_mb=0 inflight=1 bytes=7368 rid=7jwe-4a view=ask sess=7jwees51 io_calls=5 io_ms=46 io_mb=0.00 spans=- io_top=table:PATCH:albumsdirtyset(..):x1/11ms/0KB,table:PATCH:photoalbums(..):x1/10ms/0KB,table:GET:photousers(..):x1/10ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB,blob:GET:lexical-index:x1/7ms/0KB
2026-10-22 14:51:39,245 INFO perf PERF event=request method=POST path=/albums/delete-multiple status=200 ms=20.4 rss_mb=625 rss_delta_mb=0 inflight=1 bytes=182 rid=7jwe-4f view=albums sess=7jwees51 io_calls=2 io_ms=18 io_mb=0.00 spans=- io_top=table:GET:photousers(..):x1/9ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB/err1
2026-10-22 14:51:42,100 INFO perf PERF event=request method=POST path=/albums/delete-multiple status=200 ms=21.9 rss_mb=625 rss_delta_mb=0 inflight=1 bytes=182 rid=7jwe-4g view=albums sess=7jwees51 io_calls=2 io_ms=20 io_mb=0.00 spans=- io_top=table:GET:photousers(..):x1/11ms/0KB,table:GET:photoalbums(..):x1/9ms/0KB/err1
2026-10-22 14:52:00,420 INFO perf PERF event=request method=POST path=/albums/21e40bc1-764b-4fcf-a399-374cb49ae19b/photos/add status=200 ms=66.3 rss_mb=627 rss_delta_mb=0 inflight=1 bytes=2414 rid=7jwe-4j view=ask sess=7jwees51 io_calls=4 io_ms=63 io_mb=0.00 spans=- io_top=table:GET:photoalbums(..):x1/39ms/0KB,table:PATCH:photoalbums(..):x1/9ms/0KB,table:PATCH:albumsdirtyset(..):x1/8ms/0KB,table:GET:photousers(..):x1/7ms/0KB
2026-10-22 14:52:00,300 INFO perf PERF event=request method=POST path=/albums status=200 ms=33.6 rss_mb=626 rss_delta_mb=0 inflight=1 bytes=219 rid=7jwe-4i view=ask sess=7jwees51 io_calls=3 io_ms=31 io_mb=0.00 spans=- io_top=table:PATCH:albumsdirtyset(..):x1/11ms/0KB,table:PATCH:photoalbums(..):x1/11ms/0KB,table:GET:photousers(..):x1/9ms/0KB
2026-10-22 14:51:24,381 INFO perf PERF event=client_span user=owner sess=7jwees51 name=index.albums.total ms=193 view=albums rows=13 cached=True
2026-10-22 14:52:20,144 INFO perf PERF event=client_span user=owner sess=tzsqsd0p name=index.albums.parse ms=0 view=albums rows=12
2026-10-22 14:52:20,144 INFO perf PERF event=client_span user=owner sess=tzsqsd0p name=index.albums.total ms=188 view=albums rows=12 cached=True
2026-10-22 14:52:40,190 INFO perf PERF event=client_span user=owner sess=l848gzn1 name=index.albums.total ms=137 view=albums rows=12 cached=True
2026-10-22 14:53:21,489 INFO perf PERF event=client_span user=owner sess=ayfglad2 name=index.albums.total ms=59 view=trash rows=12 cached=True
2026-10-22 14:56:01,488 INFO perf PERF event=client_span user=owner sess=ayfglad2 name=index.albums.total ms=101 view=albums rows=12 cached=True
```

## Follow-up: red dress and dressed in yellow still missing

A subsequent read-only check confirms:

| Album | Source membership | Dirty marker written (Stockholm) |
| --- | ---: | --- |
| red dress | 83 photos | 16:52:00.415 |
| dressed in yellow | 123 photos | 16:55:44.534 |

Both source albums are active. Both IDs remain in albumsdirtyset with op=modified. The album job row is still status=done, updated at 16:51:46.665, before either mutation. The indexer and cluster apps report Running. The deployment has no INDEX_MAINTENANCE_QUEUE_NAME configured in the inspected indexer/cluster containers; it is using the legacy worker path. Cluster scaling includes both clustering-queue and library-ops-queue rules.

Read-only queue-properties calls report approximateMessageCount=0 for both photostore-library-ops and photostore-clustering. No messages were consumed, hidden, deleted, or enqueued. Therefore these records show stranded dirty album changes with no newer album rebuild recorded and no queue backlog at observation time. They do not indicate a backlog of album jobs waiting for a worker. Queue counts are approximate, and the process-local dirty flag itself was not inspected. No targeted rebuild, worker restart, or other recovery action was executed.
