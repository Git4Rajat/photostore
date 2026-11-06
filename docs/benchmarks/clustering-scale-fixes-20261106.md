# Clustering scale fixes and sliced qualification — 2026-11-06

This audit records the clustering fixes and final sliced qualification at 25k, 50k and 100k faces. It preserves the existing source storage model and matching policy.

## Implementation

- Load native checkpoints with FAISS `IO_FLAG_SKIP_PRECOMPUTE_TABLE`, verify the precomputed table is empty, then explicitly restore `use_precomputed_table=-1`. The serialized runtime flag is not a reliable persisted invariant. Existing checksum, index type, dimensions, metric, trained state, codebook and quantizer checks remain enforced. Loading does not train or rebuild an index.
- Validate live-face reachability with two set differences instead of correlated slot-table scans. Both clean base revisions and dirty delta revisions must still have matching face/revision/compatibility tuples. This avoids quadratic SQL work without adding persistent indexes or changing checkpoint format. At 1k/2k/4k faces, the old reachability query alone used 2.52M/10.04M/40.09M SQLite VM operations; all current database validation used 0.096M/0.193M/0.387M. These counts exclude Python/numpy work. See [the scaling probe](clustering-scale-fixed-20261106/validation-scaling.json).
- New captures bind to `canonical-faiss-v2-membership-delta`. Assigned decisions contain person metadata and an `append-face-v1` membership operation; they omit the growing `person.faceIds` snapshot. Canonical replay reconstructs membership from the frozen workspace in decision order, using the existing idempotent membership append and size guard.
- Oversized frozen baseline person memberships use bounded immutable parts and a digest. Recovery reconstructs the exact original membership string, including whitespace and Unicode, so the original manifest digest still verifies. The manifest is published only after all parts are durable. The 60,000-byte journal guard remains unchanged for both Table and file journals.
- Live materialization still reads current person state and conditionally writes with its genuine ETag. Existing source changes, curation, deletion, membership and coverage checks remain in place.
- Already captured v1 waves retain their original journal format and decision digests. New v2 waves have a different binding so older workers reject them before source effects. A rollback to older workers must allow v2 waves to finish on compatible workers; there is no automatic downgrade.

## Sliced benchmark results

All three sequential runs used 128 existing identities, deterministic noisy synthetic 512D vectors, a first cut at 4,096 faces and subsequent 120-second soft slices. The checkpoint-miss qualification guard remained enabled.

| Faces | Elapsed | Faces/s | Successful restores | Replayed faces | Peak RSS | Peak work disk | Peak journal/checkpoint disk |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25,000 | 3m48s | 109.5 | 2 | 0 | 674 MiB | 1.40 GiB | 0.91 GiB |
| 50,000 | 7m46s | 107.2 | 4 | 0 | 714 MiB | 3.27 GiB | 2.01 GiB |
| 100,000 | 22m57s | 72.6 | 11 | 0 | 1,247 MiB | 7.71 GiB | 5.56 GiB |

Every case completed and verified all expected owners, exact membership lists and member row count. Source hashes matched the final manifest in all runs; no sampled disk errors occurred. These are single local runs, not Azure time forecasts or median results. The existing matcher configuration and source writes remain unchanged.

The run still performs substantial durable journal I/O: at 100k it wrote about 4.61 GiB of journal data and fsynced 200,013 times (about two fsyncs per face). The harness counted 713,592 fake storage-interface operations, roughly 7.1 per face. Source ownership and coverage writes also remain proportional to input size. Compact membership decisions fix payload growth and the 60KB failure; they do not make calls nearly zero.

## Validation and limits

The focused related suite passes **449 tests**. No deployment files were changed for these clustering fixes. Clustering coverage includes real trained-index save/restore without retraining, exact ownership/membership comparison against the single-pass reference, Table and file journal recovery, exact large/Unicode baseline reconstruction, missing/changed part rejection, v1 continuation and older-binding rejection before source effects. New tests retain rejection of missing base/delta identity mappings and stale revisions, and bound SQLite VM work to detect quadratic validation regressions.

The source membership field still has its existing 61,440-byte append guard. This work does not migrate membership authority to member rows or remove proportional source writes. High identity concentration remains a separate matching qualification: the runtime caps overfetch at 1,024 candidates per source and conservatively suppresses an automatic merge when it cannot establish a distinct runner-up or exhaustion. This policy was not changed for performance.

The largest remaining measured cost is reopening the mounted journal. Across the 100k slices, `journal_open` consumed 383.5 seconds, because every attempt reconstructs a local SQLite index by rereading the append-only journal. Durable journal writes consumed 300.1 seconds. A persistent, integrity-checked local journal index is the next performance target; its recovery and mount-loss contract need design before changing it. The 100k mount peak was 5.56 GiB and work peak 7.71 GiB, so deployment disk sizing also needs to account for both volumes.

All timing is local macOS/ARM with fake ETag-bearing remote tables and real FAISS, SQLite, file journal/checkpoint I/O and synchronization. Upload, Vision, receipt loading, downstream indexing/publication, Azure latency and multi-worker contention are excluded. These synthetic results do not establish production throughput or face-quality/recall for every identity distribution. Resource peaks include fake source rows and are sampled; they are not isolated native-runtime RSS guarantees.

## Reproduce

```sh
# Repeat with --faces 25000, 50000, 100000, one process at a time.
.venv-photostore-tools/bin/python backend/scripts/benchmark_clustering_scale.py --faces 100000 --identities 128 --slice-seconds 120 --first-cut 4096 --fail-on-restore-miss --output /tmp/fixed-sliced-100k.json
.venv-photostore-tools/bin/python backend/scripts/benchmark_clustering_checkpoint_validation.py --output /tmp/checkpoint-validation-scaling.json
```

Inspect JSON `status`, output validation, restore hits and replay count; the benchmark records failures and returns normally so a matrix can continue. Native/workspace restore time excludes the separately timed journal opening/indexing phase. Raw completed runs are [25k](clustering-scale-fixed-20261106/sliced-128-25000.json), [50k](clustering-scale-fixed-20261106/sliced-128-50000.json) and [100k](clustering-scale-fixed-20261106/sliced-128-100000.json). The [source manifest](clustering-scale-fixed-20261106/source-manifest.json) records hashes matched by all three. The SQLite work scaling probe is [here](clustering-scale-fixed-20261106/validation-scaling.json).
