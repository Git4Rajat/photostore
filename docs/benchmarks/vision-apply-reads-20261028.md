# Vision apply read consolidation

The latest completed 195-image wave spent accumulated worker time of 17.8s in artifact capture, 17.7s in face persistence, and 17.3s in derivative promotion. These inclusive phase totals overlap across workers; they are not additive wall time savings.

This change targets multi-face persistence. It replaces individual embedding version reads with bounded key queries and removes redundant lease reads while preparing deferred writes. Every transaction still checks the live filename lease and source fence. Exact ETags protect updates, missing rows use create-only writes, embeddings publish before faces, and filename completeness publishes last.

`IPWORK_FACE_APPLY_READS_BATCH_ENABLED` defaults to `true`; setting it to `false` restores the previous read path. Zero- and one-face paths retain their existing behavior. Existing face-row reads needed to preserve user curation remain.

## Local experiment

Run `backend/scripts/benchmark_vision_apply_reads.py`. The saved JSON contains 240 runs: five alternating rounds, three variants, 0/1/8/32 faces, fresh/curated rows, and 0/10ms injected storage delay. Storage doubles enforce ETags and table transaction semantics. All variants produced identical output digests.

| Fresh faces | Previous requests | Consolidated requests | Previous median ms | Consolidated median ms |
| --- | ---: | ---: | ---: | ---: |
| 0 | 7 | 7 | 100.8 | 97.6 |
| 1 | 14 | 14 | 203.8 | 197.8 |
| 8 | 37 | 14 | 509.5 | 201.0 |
| 32 | 109 | 16 | 1576.2 | 237.1 |

Times use 10ms simulated request latency, not Azure measurements. With existing curated rows, requests fell from 44 to 21 for eight faces and 140 to 47 for 32 faces. These results support fewer storage round trips; they do not predict a whole-wave throughput improvement.

## Validation and Azure follow-up

Regression coverage includes source replacement during version reads, lease loss, concurrent face curation, embedding ETag conflicts, failed queries, partial cross-table writes, and publication ordering. The historical persistence benchmark explicitly disables the new read consolidation to preserve its original baseline.

New metrics: `face_write_version_batches`, `face_write_version_keys`, `face_write_version_reads`, `face_write_version_read_ms`, `face_write_version_read_errors`, and `face_write_lease_checks_deferred`. Runtime configuration reports `face_apply_reads_batch_enabled`.

After deploying, compare face-persistence storage requests and wall time per image, split by face count and fresh versus existing artifacts. Check version-read errors, conditional-write failures, and incomplete filename publications alongside throughput. Artifact capture and derivative promotion remain separate follow-up opportunities.
