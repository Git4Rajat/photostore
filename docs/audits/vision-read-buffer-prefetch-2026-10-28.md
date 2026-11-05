# Vision read, buffer and preview implementation

Implements the selected results from the
[matched benchmark suite](../benchmarks/vision-optimization-suite-20261028.md).
Model scheduling and the default two source workers remain unchanged.
The benchmark's slower asynchronous CLIP and stage-queue arrangements are not
enabled. Full-upload throughput gains still require a deployment and upload.

## Read layout

`table_reads.read_entities` sorts requested keys, preserves caller result order
and SDK ETags, and represents authoritative absence as `None`. Vision proof
and receipt reads additionally separate known coordination namespaces before
building the existing fourteen-key/URL-bounded filters. Ordinary filenames are
not interpreted as namespaces. Mutable metadata is read in fresh groups of at
most twenty, using parallel query batches.

Read concurrency is capped at four through a shared worker pool and semaphore.
Workers receive accounting/purpose context, not source-write or publication
fences. Failure drains the submitted batch before propagating; no partial
result is returned. Existing conditional writers and source claims stay fresh.

## Member and receipt buffers

Member retrieval fetches up to 1,000 rows, then supplies groups of twenty.
The processing/checkpoint transaction limit is unchanged. Checkpoints use the
last inspected member after draining source effects, never the fetched tail.
Buffer capacity exhaustion is not interpreted as the end of the wave; remaining
members are fetched again after the committed cursor. No library-wide metadata
scan or whole-wave startup barrier is introduced.

Buffers use memory first and private SQLite spill files on local ephemeral
storage. Only locally serialized SDK rows are decoded; externally supplied
cache files are not accepted. ETags and typed fields survive spill. SQLite has
a 4 MiB cache and no durability journal/fsync; disk state is disposable. Disk
payload admission reserves 30% for SQLite pages/index overhead, and the database
also has a physical page limit. Capacity/SQLite failures cause eviction,
memory-only fallback, or member refetch rather than success evidence.

Immutable manifests and face receipts survive page boundaries within an
attempt. Missing rows and mutable metadata/inbox/proof absence do not become
long-lived cache entries. Every receipt reference still receives its digest,
source identity and coverage validation. Cache files close on success, errors
and generator/stage exit. A new attempt rebuilds its local state.

| Setting | Default | Purpose |
|---|---:|---|
| `IPWORK_VISION_MEMBER_FETCH_SIZE` | 1000 | Member query size; permitted 20–1000 |
| `IPWORK_VISION_MEMBER_MEMORY_MB` | 1 | Serialized member payload RAM budget |
| `IPWORK_VISION_MEMBER_DISK_MB` | 64 | Member SQLite budget |
| `IPWORK_VISION_RECEIPT_MEMORY_MB` | 8 | Serialized immutable receipt RAM budget |
| `IPWORK_VISION_RECEIPT_DISK_MB` | 256 | Receipt SQLite budget |

RAM budgets permit 0–64 MiB, disk budgets 0–1,024 MiB. Python key/index objects,
SQLite's page cache and native inference allocations are additional process
memory, not part of the serialized-payload budgets. The attempt's process RSS
measurements expose the total footprint. Metadata remains fresh per processing
group instead of being treated as authority from an old whole-wave snapshot.

## Preview lookahead

Only ready photo previews for matching source/revision rows with unfinished
visual steps are eligible. The coordinator queues a bounded group; four I/O
workers overlap downloads with existing source processing. It never fetches
originals or runs models ahead of source admission.

The source processor consumes a preview only after its normal lease claim and
only when library, filename, physical photo, processing revision and file hash
match the claim snapshot. Existing JPEG/full-decode validation remains. Source
writes and completion remain fenced. Failed prefetch follows the existing
original fallback without repeating the same preview GET.

Each speculative GET requests at most 2 MiB plus one detection byte, uses one
SDK download stream, no SDK retries, and short connection/read timeouts. This
limits abandoned lookahead work. Reservations include pending and currently
consumed buffers. Disk mode stores compressed bytes and removes files when
consumed; decoded images/tensors remain bounded by the existing source slots.
Unused futures drain before buffers/context close. Failures do not establish
that an image has no data or consume a model attempt.

| Setting | Default | Purpose |
|---|---|---|
| `IPWORK_VISION_PREFETCH_ENABLED` | `true` | Enable wave preview lookahead when preview reuse is enabled |
| `IPWORK_VISION_PREFETCH_STORAGE` | `memory` | `memory` or local ephemeral `disk` |
| `IPWORK_VISION_PREFETCH_WORKERS` | `4` | One to four I/O workers |
| `IPWORK_VISION_PREFETCH_BUFFER_MB` | `16` | Reserved compressed bytes; permitted 2–64 MiB |

These defaults are in code, so an image update enables the selected behavior
without an infrastructure change. A smaller member fetch size or disabled
preview prefetch provides a runtime rollback option. Native BLAS limits and
CLIP image batching defaults are unchanged.

## Instrumentation

`vision_runtime_config` records active group/fetch/concurrency settings and
buffer limits. Attempt summaries retain wall time and process CPU time and add
current process RSS and process-lifetime peak RSS. Those memory/CPU readings
can include concurrent work in the same process; they are not exclusive image
measurements.

New measurements include:

- Member fetch calls, fetched rows/groups, metadata row/byte peaks, and separate
  elapsed timers for member/metadata/proof-inbox/manifest/face-receipt retrieval.
- Batch wall time, accumulated query service/queue time, errors and maximum
  observed in-flight reads. SDK counters retain actual request counts,
  response bytes, missing rows, throttling and other HTTP status classes.
- Member/receipt memory payload, disk payload, disk-file and entry-count peaks;
  disk reads/writes/errors, cache hits/misses/evictions and buffer refetches.
- Preview submitted/consumed/missed/error/unused counts and bytes; accumulated
  download time, consumer wait, drain time, disk bytes and reservation/slot peaks.
  `preview_prefetch_transfer_bytes` counts returned SDK bodies, including a
  too-large preview rejected after the bounded range GET.
- Face-index source preparation time, previously included only in total wall
  time, now separately attributed as `vision_source_prepare_ms`.

Peak observations take maxima, not sums. Timers are inclusive: do not add
member/metadata fetch timers to their enclosing page-load time, nor sum nested
inspection timers with their parent. Parallel SDK/download service totals
can exceed wall time. Background prefetch/read I/O is attributed to the wave;
consumer wait remains attributed to the image. No filenames, photo contents,
coordinates, OCR text, vectors, secrets or cache keys are emitted.

## Validation

Tests cover sorted namespace grouping, ETag preservation, parallel read failure
draining, bounded buffers/spill, disk failure, complete member enumeration under
capacity pressure, deadline resume at the last processed cursor, preview identity
changes, oversized/missing previews, range-GET limits, RAM/disk cleanup,
prefetched-image reuse and single original fallback. Existing wave recovery,
conditional-write, receipt validation, clustering and publication tests run
alongside these tests.

The full backend suite had 54 existing failures/errors reproduced in an archive
of committed HEAD: legacy live-FAISS fixture cache references, lexical fixtures,
two library-ordering cases, upload hash snapshot behavior, face-scan timeout and
an older persistence-phase expectation. These are outside this change. Initial
additional failures exposed a stricter unrequested-key check; the helper now
retains its original behavior of ignoring extra same-partition rows while still
rejecting foreign partitions. All related tests passed after that correction.
The final full run excludes only the 54 independently reproduced baseline cases.
It completed with **4,154 passed, 14 skipped, 54 deselected**. Focused buffer,
preview, coordination, ownership, publication and diagnostic checks also pass.
No production deployment, commit or push is performed by this implementation.
