# Vision optimization benchmark suite — 2026-10-28

## Scope and interpretation

The suite compares read layout, member paging, metadata buffering, preview
prefetch, model scheduling, and inspection/model overlap. No production data,
app configuration, scaling, queue dispatch or role assignment is changed.
The benchmark creates a temporary private storage account and a manual Container
Apps job in the existing forenklaprep1 environment, then deletes those resources.

Azure uses the currently deployed d8c8248 worker by immutable manifest digest
`sha256:73c70ad8de009b858e4bace47182fdc48aea2b2aab7fb6e0fef8f1d013226f3a`,
2 CPUs, 4 GiB memory, OpenBLAS/OpenMP thread limit 1, and a hash-recorded
source overlay. This is an isolated experiment over the production worker's
dependencies, not a deployment of a new application version.

The small wave has 18 members; the large wave has 6,739. Their historical photo
metadata is no longer present. Present-metadata timings therefore use 218
explicitly synthetic rows in the temporary account, bound to historical member
keys, with roughly 1 KiB of padding per row. Production proofs and retained
manifests/face receipts remain real. Missing completion inboxes are measured as
missing; manifest/face tests sample retained library receipts when wave inbox
references are unavailable. These samples do not recreate the original upload.

All comparisons use at least three rounds with alternating ordering; scheduling
uses four. Results below are medians. SDK response counters include all observed
responses, including authoritative 404s. Byte counts are response bodies, not
total wire traffic. Read results are compared by complete payload fingerprint.

## Inspection reads in Azure

The 200-member comparison contains 600 proof/inbox keys, 200 manifests and
181 face receipts. The proof sample has 200 existing rows and 400 absences;
all sampled manifests and face receipts exist. All 216 read configurations
returned matching fingerprints, without observed throttling or non-404 errors.

| Workload | Existing sequential queries | Four parallel query batches | Sorted, separate key families | Four parallel sorted/family batches | Eight parallel point reads |
|---|---:|---:|---:|---:|---:|
| Synthetic metadata, 200 keys | 253.5 ms | 82.1 ms | Not applicable | Not applicable | 337.0 ms |
| Proofs/inboxes, 600 keys | 24,331.2 ms | 6,828.3 ms | 679.9 ms | 182.6 ms | 1,087.5 ms |
| Manifests, 200 keys | 266.7 ms | 68.0 ms | 198.7 ms | 89.4 ms | 680.4 ms |
| Face receipts, 181 keys | 3,138.9 ms | 901.9 ms | 455.6 ms | 142.4 ms | 369.8 ms |

For proofs, sequential/current queries use 43 responses; separated families use
45; point reads use 600. For face receipts, both query layouts use 13 responses;
point reads use 181. Thus faster reads need not mean more point requests.

`prefix_query` sorts keys and groups by the component before `!`, then uses
the existing bounded OR query batches. `parallel_prefix_query` runs at most
four such batches concurrently. For face receipts, sorting is the relevant
change because all keys already share one family. The prototype prefix mode
overgroups ordinary filenames into individual queries and must not be applied
to metadata filenames; its metadata results are intentionally excluded above.
An implementation should recognize known coordination namespaces explicitly.

The earlier deletion-only benchmark did not expose this issue. Mixed key
families and unsorted face receipt keys are substantially more expensive in
this expanded measurement. This demonstrates a query-layout problem; it does
not independently prove the server's internal execution plan.

## Inspection and real model work together

This replay combines 48 historical member proof-key sets with an independent
48-image model validation corpus. It runs the real OCR, face and CLIP processors
with two source workers. It does not process the historical members' images or
execute claims, persistence, qualification, freezing or publication.

| Arrangement | Wall time for 48 inputs | Inspection elapsed total |
|---|---:|---:|
| Current mixed proof queries, then processing | 17.064 s | 6.080 s |
| Sorted/separate proof queries, then processing | 11.242 s | 0.279 s |
| Sorted/separate proof queries, next page overlaps processing | 11.196 s | 0.317 s |

All 432 timed model passes and inspection payloads match. Grouping reduces
wall time by approximately 34% in this replay. Additional overlap changes the
median by only 0.4%, too small to treat as an established improvement here.
Overlapped inspection totals cannot be subtracted from elapsed wall time.

## Whole-wave member fetching

The range scan reads every durable member, streaming and hashing the complete
payloads. It measures fetching, not metadata inspection or model work. Larger
fetch pages can still feed independent 20-member processing/checkpoint groups.

Developer-workstation results for the 6,739-member wave:

| Service page size | Requests | Full scan |
|---|---:|---:|
| 20 | 337 | 19.285 s |
| 200 | 34 | 4.088 s |
| 1,000 | 7 | 2.695 s |

All layouts return the same rows and roughly 5.85 MB of response bodies. The
18-member wave uses one request at every page size. Larger pages increase time
to the first processing chunk, so streaming should begin as each page arrives
instead of waiting for a whole-wave barrier. These numbers are not the prior
89-second application page-load metric, which also included metadata reads.

Azure results for the complete same wave:

| Service page size | Requests | Full scan | First 20 members available |
|---|---:|---:|---:|
| 20 | 337 | 5.561 s | 11.9 ms |
| 200 | 34 | 1.461 s | 57.2 ms |
| 1,000 | 7 | 1.222 s | 240.4 ms |

For the 18-member wave, all page sizes take one request and approximately
11–13 ms. A larger fetch page is useful for large waves and does not add
extra requests to this small wave. The 1,000-row page saves about 78% of the
full member-scan wall time, with a roughly 230 ms later first processing chunk.
Neither measurement requires waiting for all wave metadata before processing.

## Metadata buffer capacity

The capacity experiment uses generated metadata, not 100,000 real user photos.
Each mode runs in a fresh process. SQLite uses a 4 MiB cache, an indexed integer
key, batches of 1,000 inserts, and 20-row planning reads. Journaling and fsync
are disabled because this is a rebuildable local planning cache.

Developer-workstation first-round results:

| Rows | Mode | Ingestion + planning | Sampled process RSS peak | Disk |
|---|---|---:|---:|---:|
| 20 | Memory | <1 ms | 22.7 MiB | 0 |
| 20 | SQLite | ~1 ms | 23.8 MiB | 48 KiB |
| 1,000 | Memory | 8 ms | 24.9 MiB | 0 |
| 1,000 | SQLite | 11 ms | 28.1 MiB | ~2 MiB |
| 100,000 | Memory | 0.758 s | 228.8 MiB | 0 |
| 100,000 | SQLite | 1.032 s | 32.8 MiB | 195.8 MiB |

All planning fingerprints and synthetic original-required counts match. Those
counts are generated flags, not validation of real EXIF packets or previews.
Actual metadata size, native allocations and OS page cache affect capacity;
the disk result is a viable buffer experiment, not a production RAM guarantee.

Azure capacity results, medians over three isolated processes per case:

| Rows | Mode | Ingestion + planning | Maximum sampled RSS across rounds | Disk |
|---|---|---:|---:|---:|
| 20 | Memory | 0.5 ms | 18.7 MiB | 0 |
| 20 | SQLite | 1.1 ms | 19.4 MiB | 48 KiB |
| 1,000 | Memory | 15.6 ms | 20.4 MiB | 0 |
| 1,000 | SQLite | 21.2 ms | 23.0 MiB | ~2 MiB |
| 100,000 | Memory | 1.563 s | 173.1 MiB | 0 |
| 100,000 | SQLite | 2.050 s | 26.6 MiB | 195.8 MiB |

SQLite saves approximately 85% of peak process RAM at this 100k-row shape,
with about half a second extra ingestion/planning time. It provides capacity;
it is not faster than memory. A threshold or byte budget can keep small waves
in memory while spilling large snapshots to local ephemeral disk.

## Model scheduling and preview prefetch

The same 48 worker previews are used throughout: 45 distinct preview byte
hashes, portraits, video frames and four text controls. Model loading/preview
generation are excluded, warmup is separate, and shared RGB is decoded before
parallel stage use. Active decoded jobs are bounded to two, or four in the
explicit four-image stage experiment. Face detection/alignment/embedding remain
inside the existing dependent face processor.

Local M4 Pro scheduling medians:

| Arrangement | Wall time, 48 images |
|---|---:|
| OCR → face → CLIP, two source workers | 3.280 s |
| CLIP → OCR → face, two source workers | 3.254 s |
| Asynchronous CLIP, one CLIP worker | 2.777 s |
| Asynchronous CLIP, two CLIP workers | 2.775 s |
| Separate single-worker stage queues, two active images | 3.518 s |
| Separate single-worker stage queues, four active images | 3.392 s |

All 1,152 timed local image passes have identical complete output hashes,
including face data, OCR text and CLIP vectors/decisions. The Mac can consume
more than two CPU cores despite the two-source-worker setting; its 15% async
improvement must not be assumed to transfer to a two-CPU Azure allocation.

Azure scheduling results, same two-CPU allocation throughout:

| Arrangement | Wall time, 48 images | Process CPU time | Maximum sampled RSS |
|---|---:|---:|---:|
| OCR → face → CLIP, two source workers | 10.869 s | 21.159 s | 2,293.5 MiB |
| CLIP → OCR → face, two source workers | 10.953 s | 21.197 s | 2,239.2 MiB |
| Asynchronous CLIP, one CLIP worker | 12.207 s | 24.174 s | 2,241.1 MiB |
| Asynchronous CLIP, two CLIP workers | 12.450 s | 24.751 s | 2,240.6 MiB |
| Separate single-worker stage queues, two active images | 12.881 s | 23.270 s | 2,235.8 MiB |
| Separate single-worker stage queues, four active images | 12.893 s | 23.754 s | 2,257.4 MiB |

All 1,152 timed Azure scheduling passes match complete output hashes, with
17 accepted faces and seven text-positive inputs in every configuration.
The observed cgroup quota is `200000 100000`, or two CPUs. Baseline process CPU
time divided by wall time is approximately 1.95 cores, about 97% of the quota
in this isolated core workload. It already overlaps work across two photos.
Additional scheduling increases CPU time and wall time on this allocation;
asynchronous CLIP is 12–15% slower and separate stage queues roughly 19% slower.
Keep the current scheduling for this allocation. These CPU utilization figures
describe the offline core benchmark, not the live app's complete I/O pipeline.

Preview tests download the same 48 private benchmark JPEGs, 13,129,779 bytes,
and verify every SHA-256 before and after buffering. The queue permits four
downloads and reserves at most 16 MiB, with approximately 3.38 MiB occupied
on this corpus. Disk buffering removes consumed files. Fetch-only tests do
not include model inference or production source selection.

Local Azure-SDK connection-reuse medians: serial 5.406 s, four RAM-buffered
downloads 3.513 s, four disk-buffered downloads 3.654 s. An additional urllib
run is retained separately; its connection setup differs from the app SDK and
must not be used as the primary performance recommendation.

Azure SDK connection-reuse medians are 0.689 s serial, 0.385 s with four RAM
downloads, and 0.334 s with four disk downloads. This is approximately 44–51%
less fetch-only wall time. The corpus occupies only about 3.38 MiB of queued
compressed payloads, so it does not require disk to fit in memory. The small
disk/RAM difference is not evidence disk is intrinsically faster. Use disk
for capacity or buffering policy, and preserve byte limits in either mode.

## Completion checks

All defined components are complete: 216 read configurations, 24 scheduling
passes, 18 capacity cases, nine combined replays, 18 full-wave scans and nine
SDK-prefetch cases. The first execution completed reads, preview transport
controls and the combined replay, but scheduling/capacity stopped before
producing results because psutil is absent from the worker image. Their
corrected Linux sampler reads `/proc/self/statm`; no dependency installation
or image change was needed. The follow-up execution succeeded. Failed or
incomplete component runs are excluded from performance conclusions.

The verification script confirmed identical read payloads, scan fingerprints,
planning results and preview input hashes across matched variants. All 1,584
timed Azure core/replay image passes and 1,152 local scheduling image passes
match their respective per-image reference outputs across variants. This does
not assert bit-for-bit equality across different hardware platforms. Source
hashes match the tested overlays.

Existing focused regression checks: 53 passed. New benchmark correctness is
checked against matched payload/model hashes and buffer download hashes.
The suite does not establish a full-upload images/hour result. That still
requires deploying selected changes and observing a fresh matched upload.

Temporary job `vision-all-9840a137e4` and tagged storage account
`psbench71a9b27cca24` were deleted; absence of both was verified at
2026-10-28 11:39:53 UTC. No production deployment or role change was performed.

Raw Azure artifacts: [reads](vision-reads-azure-20261028.json),
[scheduling](vision-scheduling-azure-20261028.json),
[buffer capacity](vision-buffering-azure-20261028.json),
[whole-wave scans](vision-wave-scan-azure-20261028.json),
[SDK preview downloads](vision-previews-sdk-azure-20261028.json),
[combined replay](vision-handoff-azure-20261028.json), and
[suite verification](vision-suite-verification-20261028.json).
The [environment record](vision-suite-environment-20261028.json) includes tested
source hashes, cleanup proof and confirmation that the live Vision template
is unchanged.

## Recommended implementation order

1. Sort requested coordination keys, separate known proof/receipt namespaces,
   and use at most four concurrent read batches. Apply namespace grouping to
   coordination keys, not arbitrary filenames. Preserve entity ETags, explicit
   absence results and reference validation. This is the strongest measured win.
2. Fetch larger contiguous wave-member pages while retaining 20-member commit
   groups and the existing two active source workers. Process the first chunk
   promptly; keep server ownership/claim checks authoritative and fresh.
3. Use byte-bounded metadata/immutable receipt caching. Keep small waves in
   memory; use SQLite when snapshot size justifies disk. Do not retain mutable
   proof/inbox absence as permanent evidence. A local snapshot does not remove
   its initial network fetch cost or replace conditional source writes.
4. Add bounded preview prefetch when live download timings justify it. The
   fetch-only savings here are small in absolute seconds for 48 inputs and
   should not be promoted as a model-processing speedup.
5. Keep the current two-source-worker model schedule on two CPUs. Do not enable
   asynchronous CLIP or independent stage queues based on the Mac result.

Inspection overlap can remain an optional later experiment: after query layout
is fixed, it did not provide a material additional gain in this matched replay.

## Earlier completed experiments

The suite supplements the existing matched experiments rather than rerunning
unchanged model variants:

- [Core batching in Azure](vision-core-batching-azure-20261027.md): standalone
  CLIP batches are faster per image, but opportunistic two-image batching made
  the two-worker complete pipeline 2.4–3.3% slower. YOLO batches also lost to
  single-image detection on the two-CPU worker.
- [Local model microbatches](vision-core-microbatches-local-20261027.md): shared
  decode, source concurrency and individual model batches have matched output
  validation. These results also demonstrate why local gains need Azure checks.
- [Native thread limits in Azure](vision-stages-azure-20261027.md): the prior
  OpenBLAS cap comparison and two-versus-more-source-worker experiments remain
  the basis for retaining one native BLAS thread and two source workers.

This does not exhaust every possible algorithm or model export. In particular,
OCR resolution changes need labelled small-text quality tests, and batching the
fixed-single-input face embedding model needs a separately validated export.
