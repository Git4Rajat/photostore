# Persistence-to-qualification audit on fixed 2 CPU / 4 GiB

## Findings

The latest clean 666-image run spent 290.74 seconds in the source pool and
49.94 seconds in serial qualification/settlement out of 358.16 attempt seconds.
Mean source apply was 534.59ms/image. Face persistence was 149.50ms, capture
73.45ms and completion delivery 58.53ms. Worker intervals overlap; these values
cannot be added to qualification wall time to forecast savings.

### Batched queries can increase service work

`table_reads.read_entities` builds a partition filter plus up to fourteen
`RowKey eq ...` conditions joined with OR. Grouping namespaces keeps families
separate but does not convert this into an indexed range or point query.
Microsoft explicitly documents that this shape performs a partition scan:
[Azure Table query design](https://learn.microsoft.com/en-us/azure/storage/tables/table-storage-design-for-query).

Receipt keys are hash-based and scattered within a library artifact partition.
Fewer HTTP requests therefore do not guarantee less service work. In the clean
run, face receipt fetch phases totaled 22.92 seconds; the first 166-image pass
accounted for 13.96 seconds. Cache disk I/O was zero, batch queueing totaled
about 0.95 seconds, and no throttling/server errors were recorded. Query shape
is a concrete concern and a plausible explanation, not yet a causal attribution
of every second of that production interval.

### The same artifacts cross multiple read/write boundaries

| Boundary | Current work | Safe optimization boundary |
| --- | --- | --- |
| Face persistence | Acquire filename writer; inspect prior curation; write embedding and face conditionally; publish complete lookup | Preserve original artifact ETags and live source/filename ownership |
| Capture | Read lookup; read just-written face/embedding; create immutable snapshots/manifest; recheck lookup | Potentially reuse exact committed immutable outputs; validate lookup generation/curation races |
| Completion acceptance | Read immutable manifest/snapshots and validate them; create durable inbox under policy guard | Request-scoped committed receipt reuse can remove readback; retain scope/hash/coverage checks |
| Metadata ACK | Conditional acknowledgement using commit ETag; conflict falls back to fresh metadata | Already optimized; retain race handling |
| Coordinator qualification | Refetch mutable metadata/proofs/inboxes; prefetch immutable receipts; check contract/model coverage | Carry bounded immutable receipt payloads from completed workers; retain current metadata/policy and outcome gates |
| Final publication | Freeze wave evidence and qualify downstream work | Keep durable receipts for recovery and downstream consumers |

Code: `storage_utils._store_client_face_entities`, `_commit_worker_processing_results`,
`processing_receipts.ArtifactReceipts.capture/verify_reference`,
`upload_waves.ShadowWaves.accept_source_completion`,
`processing_completion.relay_completion`, and
`upload_wave_execution._process_vision_group/_observe_qualified`.

The prior isolated one-face fixture made 26 SDK-method calls/image for face
persistence, capture, metadata commit and inbox acceptance. Its breakdown was
six lookup reads, six metadata reads, two embedding reads, one face read, two
lookup updates, two face/embedding creates, three job transactions, three job
reads and one metadata update. This excludes actual source claim/release,
derivative transport, ACK and whole-wave qualification; it is not the live
43.22-call/image counter. Its receipt-only prototype removed two reads/image
and showed consistent component gains; those gains are not full-upload gains.

## Implementable order

1. Measure and fix receipt query shape first. Compare identical bounded groups
   through current OR queries and four concurrent point reads in populated
   disposable Azure partitions. Include missing keys, payload size, HTTP page
   responses and output equality. Apply narrowly to the affected reader first;
   do not blindly change every shared-table caller.
2. Reuse successfully committed immutable receipt rows in capture/delivery.
   Cache only after create success or an existing identical row is proved.
   Scope by table/partition, source identity and attempt; bound bytes/count and
   release in `finally`. Never cache negative reads, cleanup policy, leases or
   mutable metadata. Continue validating hashes and source binding.
3. Carry those immutable payloads back from source futures to coordinator
   qualification after durable completion acceptance. Currently futures return
   no evidence and their result is discarded. A bounded explicit evidence
   return avoids shared mutable cache internals between source threads. A lost
   cache or a different delivery must fall back to durable reads. Retain fresh
   source-state/metadata checks, mutable inbox/proof semantics and final freeze.
4. Only then prototype committed face/embedding write reuse during capture.
   Unlike immutable receipts, these rows are mutable: caching arbitrary reads
   can lose user curation. Define whether the receipt represents the exact
   committed inference output or the latest mutable face row before changing
   reads. Preserve filename-generation validation and CAS rejection behavior.

Required fault cases include failed/ambiguous receipt creates, conflicting
payloads, source replacement/new lease after prefetch, missing/corrupt receipts,
curation races, lookup-generation changes, cleanup invalidation, lost inbox/ACK
responses and cache overflow/fallback. Normal successful-path HTTP reductions
alone do not establish correctness.

## Larger redesign, if incremental changes are insufficient

A single immutable source-generation face representation with mutable curation
stored separately could eliminate writing face output into two mutable tables
and then copying it into receipt snapshots. Source metadata would publish the
complete generation reference conditionally. This requires browser/clustering
read adapters, stable face identity on reprocessing, deletion/retention design
and migration; it is an architecture option, not a quick cache change or a
measured performance promise. No production implementation is made in this audit.

## Measurement limits

Reducing all 49.94 seconds of qualification would save only 13.9% of this run;
the 10k/hour target needs about 33.1% total wall reduction. Source CPU/I/O work
must improve too. No infrastructure scaling or production configuration change
is part of this work. INT8 and query-shape benchmark results are recorded
separately after measured runs complete.

## Completed experiment outcomes

- [Actual Azure query benchmark](../benchmarks/vision-receipt-query-shape-azure-20261029.md):
  at 50,000 receipt rows, four bounded point reads at a time reduce a 20-key
  group's median wall from 1,688ms to 68ms. At 1,000 rows they are slower
  (20ms to 105ms), so avoid a blanket strategy change. All values/absence checks
  match. This supports prioritizing large-partition scans and immutable reuse.
- [Actual 2-vCPU/4-GiB INT8 benchmark](../benchmarks/vision-int8-azure-20261029.md):
  face and CLIP component walls decrease 63%/60%, and both combined kernels
  decrease 61%, but face neighbor results and CLIP retrieval results change
  materially. These quantization candidates are rejected for deployment.
- Both temporary jobs and their private storage account were deleted; absence
  was verified. No production code, roles or application settings changed.
