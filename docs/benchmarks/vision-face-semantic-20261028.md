# Face persistence and semantic batching: local experiments

28 October 2026. Benchmark scripts and tests only; production behavior unchanged.

## Face persistence

The script calls the production `_store_client_face_entities` path using the
repository's ETag-aware table doubles, with deterministic 512-dimensional face
embeddings. Each scenario runs five alternating rounds, for fresh output and
existing user-curated output, at zero and simulated 10 ms request latency.
There are **180 runs** across zero, one and eight faces.

Variants:

- **Current:** unchanged production row writes and source checks.
- **Batch only:** enqueue conditional artifact writes, keep per-artifact source
  checks, then check the source again before each per-table transaction.
- **Batch + guards:** source check immediately before each per-table transaction,
  retaining normal lookup acquisition/publication checks. Embedding and face
  tables remain separate transactions. Complete filename lookup is published last.

Prototype interception exists only inside a `pytest.MonkeyPatch` scope and restores
all methods/context afterwards. It does not change production source files.
Each transaction is limited to 100 unique keys in one partition. Original ETags
or create-only operations are retained, with no wildcard overwrite/retry.

### Request counts

| Faces | Current, fresh | Batch only | Batch + guards |
|---:|---:|---:|---:|
| 0 | 7 | 7 | 7 |
| 1 | 14 | 16 | 14 |
| 8 | 63 | 51 | **35** |

For eight existing curated faces: **70 → 58 → 42** requests. Source metadata
checks fall **18 → 4** with batch guards. The remaining four checks are lookup
acquisition, one embedding transaction, one face transaction and final lookup
publication. Filename lease reads remain in place.

At simulated 10 ms/request, eight fresh faces took **860 → 691 → 482 ms**;
eight existing curated faces took **940 → 795 → 573 ms**. The full candidate
reduced these modeled wall times by **44% / 39%**. These are sensitivity results,
**not Azure measurements**. Local zero-delay eight-face times were approximately
one millisecond; small differences are not useful production speedup estimates.
One-face batching had no request reduction; the extra-check control regressed.

### Correctness

Canonical face rows, embedding rows and filename membership matched current
output in all scenarios, including preserved person assignment and confirmed
curation. Added tests check source changes before batch submission, concurrent
curation/ETag conflict, no complete lookup after failure, and all-or-nothing
validation of a single table transaction. Together with production fence,
filename-index and semantic-batcher tests: **137 passed**.

The table double cannot establish Azure transaction limits/behavior under
network failure. There is no cross-table atomic transaction: embedding writes
may succeed before face writes fail; the lookup must remain incomplete/dirty.
A source check is still a pre-write check, not an atomic transaction with source
metadata. The experiment does not establish stronger source fencing than the
current architecture. Real SDK conflict/recovery tests are required before
shipping the guard-coalescing candidate. Never reduce this to only one final
source check after all mutable writes.

## Semantic text embeddings

Uses the **real cached CLIP ViT-B-32 QuickGELU checkpoint**, CPU, one PyTorch
inference thread, OpenBLAS one thread. Hash fallback is rejected by the output
shape/parity checks. Thirty-two synthetic photo descriptions include empty,
short and longer trailing text. No private image/OCR metadata is used.

Three rounds, batch limits 1/2/4/8, plus production `TextBatcher` arrival tests
with two blocking source workers. Total **1,152 timed text embeddings**. Each
output matches the real-model reference: minimum cosine **0.99999988**, maximum
absolute component difference **7.16e-7**. Process-lifetime peak RSS including
model initialization was **1.70 GB decimal**; not a per-variant memory comparison.

### All texts already ready: capacity experiment

Median time to encode 32 descriptions:

| Batch size | Wall | CPU |
|---:|---:|---:|
| 1 | 384 ms | 383 ms |
| 2 | 261 ms | 341 ms |
| 4 | 233 ms | 304 ms |
| 8 | 224 ms | 297 ms |

Batch eight gives **42% lower text-only wall time** when all inputs are ready.
This is not achievable just by raising the current broker limit: only two source
workers submit requests, and each waits for its own result.

### Current two-source architecture

Five-millisecond collection window, actual production broker implementation:

| Mode | Back-to-back requests: wall | 100 ms synthetic work between requests: wall |
|---|---:|---:|
| Direct singleton, no broker | 389 ms | **1,990 ms** |
| Broker limit 2 | **263 ms** | 2,175 ms |
| Broker limit 4 | 376 ms | 2,280 ms |
| Broker limit 8 (current default) | 382 ms | 2,232 ms |

In the dense scenario all broker batches contained two texts. Limit two flushes
as soon as both arrive, whereas limits four/eight wait for the collection window
to expire without receiving a third request. Limit two cut dense-case wall time
**31% versus the current limit eight**.

With 100 ms source gaps, broker batches contained only one or two texts and all
brokers were slower than direct calls: limit two by **9%**, limit eight by **12%**.
The gap models arrival spacing, not the CPU and model-lock contention of actual
OCR/face/image-CLIP processing. The descriptions are synthetic, and timing is on
macOS CPU rather than the Azure worker's Linux/CPU quota. No whole-Vision gain
can be inferred from these percentages.

## Decision

1. Implement the previously benchmarked identity-bound preview reuse first.
2. Face batches are promising for multi-face photos; keep a direct zero/one-face
   path. Validate the proposed per-table ETag transactions and failure ordering
   with real Azure storage before enabling them. The candidate still leaves
   per-face lease and existing-row reads to optimize separately.
3. Do **not** increase semantic batch size to four/eight. Limit two is a useful
   Azure experiment under the current two-source architecture, but sparse
   arrivals may favor direct encoding. Add actual batch occupancy, collection
   wait and model-lock/service timing to compare those options on a real upload.
4. A detached semantic queue could fill larger batches, but would require bounded
   result buffering and changes to source lifetime/commit ordering. These results
   do not justify that extra architecture yet.

Raw reports:
[face persistence](vision-face-persistence-local-20261028.json),
[semantic inference](vision-semantic-local-20261028.json).

Reproduce (the face benchmark depends on the repository test dependencies):

```sh
OPENBLAS_NUM_THREADS=1 .venv-photostore-tools/bin/python \
  backend/scripts/benchmark_face_persistence.py --output /tmp/face-persistence.json

OPENBLAS_NUM_THREADS=1 HF_HUB_OFFLINE=1 \
OPENCLIP_PRETRAINED=/path/to/cached/clip.safetensors \
.venv-photostore-tools/bin/python backend/scripts/benchmark_semantic_batches.py \
  --output /tmp/semantic-batches.json
```
