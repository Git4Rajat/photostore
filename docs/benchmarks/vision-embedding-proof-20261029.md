# Vision immutable embedding-proof experiment — 2026-10-29

## Question

Can the dominant one-face persistence/capture path become materially faster by
reusing its successfully committed embedding vector rather than rereading the
mutable embedding row during artifact capture?

A filename generation alone cannot justify caching mutable face rows. Labeling
and person-merge/rejection routes in `app.py` can update curation directly without
acquiring a filename generation. The candidate therefore keeps face, lookup,
source, curation and cleanup-policy reads/checks fresh.

## Benchmark-only candidate

The embedding table transaction conditionally writes the existing compatibility
row and create-only immutable `embedding-proof!<sha256>` row together. Both have
the same partition. Only a successful transaction adds the serialized proof to
the private trial cache. The ordinary face write publishes the proof ID as an
additional field. Capture reads that face freshly, checks the referenced proof's
hash/scope and applies all original face/embedding source identity, vector,
lookup-generation, receipt hash/coverage and source checks. With no proof pointer
it uses the existing embedding read. Cache overflow reads the immutable proof
from storage and validates it; ambiguous write failures never populate cache.
Duplicate immutable proof conflicts require durable payload equality and retain
the original mutable artifact ETag. There is no ETag upgrade/retry against a
newer mutable artifact.

This saves **one read per fresh one-face image**, changes a simple embedding
write into a two-operation transaction and retains an extra durable vector copy.
Thus fewer reads may still fail to improve elapsed time.

Only single-face sources participate; zero-, two- and eight-face paths retain
existing behavior. Cache payloads are capped at 2 MiB; auxiliary key maps are
bounded by this harness's image count, not a production-ready wave cache.

## Contract and limits

This is **not** a safe shortcut for caching today's mutable embedding rows.
It introduces a canonical immutable proof pointer. Any writer that changes an
embedding must publish an updated pointer or invalidate it; legacy backfills and
cleanup/migration tooling need an explicit audit before production use. All
referenced proofs need retention, while orphan proofs need garbage collection.
The compatibility embedding copy must not silently override a referenced proof.

Fresh face curation remains authoritative. The benchmark injects a label update
without changing lookup generation and verifies the captured face includes it.
It also checks byte-cap fallback, failed-write cache exclusion, payload integrity,
cross-library/face scope and output parity. Local receipt/fence regression suites
are included: **63 tests passed**.

## Azure setup

Disposable West Europe Container Apps job, latest Vision image `b7a5222`,
**2 CPU / 4 GiB**, three source threads, five isolated temporary tables, no
production storage credentials, identity or mounts. No application changes.

Three scenarios: zero-face control, fresh one-face images, and curated
same-attempt reprocessing. Four alternating-order rounds, 32 images/comparison,
production committed receipt cache enabled in both variants. Seeded reprocessing
uses the same source revision/attempt as the existing harness and intentionally
exercises duplicate-proof handling; it is not a benchmark of a new-attempt
curated re-detection.

The timed component contains face persistence, artifact capture, metadata CAS
and completion acceptance. Models, semantic refresh, derivative promotion,
queue work, actual wave orchestration and setup/readback are excluded. The
harness runs persistence then capture in groups of eight. Component speedup is
not full-upload throughput speedup.

## Results

**24 comparisons / 768 source passes**, all output-parity checks passed. These
are median paired component wall reductions against production in the same
round, not ratios of independently calculated medians or full-upload gains.

| Scenario | Paired median reduction | Per-round range | SDK calls/image, baseline → proof |
| --- | ---: | ---: | ---: |
| Fresh one-face images | **+0.05%** | -6.6% to +15.2% | 24 → 23 |
| Same-attempt curated reprocessing | **-24.2%** | -30.8% to -3.9% | 25 → 26 |
| Zero-face unchanged control | -0.8% | -17.5% to +8.5% | 14 → 14 |

Fresh one-face medians were 2.822s versus 2.796s per 32 images. Their ratio
suggests 0.9%, but paired median reduction is only 0.05%; do not substitute the
more favorable statistic. Median paired phase changes were **93ms more face
persistence** and **111ms less capture/delivery per 32 images**. They show the
tradeoff: the added proof-write transaction largely cancels the avoided read.
The trial cache held approximately 123KB for 32 proofs.

Same-attempt reprocessing exercises duplicate immutable proof conflicts. The
candidate retains the original mutable artifact ETag, checks the already-durable
proof and retries only the unchanged conditional operation. This adds calls and
was slower in every round. New-attempt curated re-detection was not measured.

The zero-face unchanged control has broad jitter; four rounds do not provide a
tight confidence bound. There is no evidence of the material general speedup
needed for the 10k/hour target, and no basis to extrapolate a full-wave gain.

## Decision

**Do not deploy this prototype.** Saving one read is not sufficient to offset
proof publication, serialization and conflict handling. The new pointer contract,
extra durable vector copies, retention/garbage collection, and legacy-writer
migration add complexity without a demonstrated throughput benefit.

Keep the production receipt reuse already deployed. Treat this result as evidence
against adding an immutable embedding sidecar solely to avoid capture readback.
Any further artifact experiment should remove an entire publication/verification
round trip while preserving fresh curation, rather than exchange one read for a
more expensive write.


Independent durable readback verified **768 source completions, 512 face snapshots, 2,048 receipt/inbox rows and 256 face-to-proof links / immutable embedding proofs**, including hash, scope, source identity and compatibility-vector parity.

Temporary Azure job and storage account deleted; absence verified at 2026-10-29T22:38:24.051361+00:00. No production configuration, code or roles changed.
