# People index: completed build but unavailable page

`GET /api/persons/page` is served by the extras role. It reads the people
index from blob storage (or the shared disk cache), not the worker's in-memory
snapshot. A `done` job row alone does not prove another process can read it.

## Reproduced read-path defect

The worker publishes gzip JSON with `Content-Encoding: gzip`. Azure Blob SDK
downloads default to `decompress=True`, returning JSON bytes. Shared-cache
write-through files can instead contain the original gzip bytes. The old reader
always called `gzip.decompress`, swallowed decoding errors, and returned no
index. Consequently a cold extras process could report `index-missing` despite
a successful worker build.

The reader now checks gzip magic bytes and accepts both representations. Invalid
payloads still fail safely, but download/decode failures are logged separately.
Already-published valid indexes need no rebuild solely for this decoding fix.

## False-ready publication failures

People-index refresh previously swallowed data and manifest upload errors and
populated its local cache anyway. It now requires both clients and successful
data-then-manifest uploads before publishing to the process cache. Exceptions
reach priming and queue retry handling. Failed priming overrides old manifest
readiness in progress notifications; full builds also propagate failed kinds.

This does not make the two blob writes transactional or remove concurrent
writer races. Stale valid indexes continue to be served while refresh runs.

## Rollout and investigation

- Deploy the backend image to **extras** for the read fix and **worker** for
  truthful publication/job status. No frontend or infrastructure change needed.
- If a page remains unavailable, check extras logs for `People index data
  download failed` or `People index data decode failed`, and worker logs for
  `Failed to publish people index`.
- Check both roles use the same storage account, index container, and library
  id. Verify extras has read access to the published data blob.
- Local tests reproduce both raw-gzip and SDK-decoded JSON reads after clearing
  the process cache, and failures of either publication write. These establish
  code defects, not independent confirmation of a particular live deployment.