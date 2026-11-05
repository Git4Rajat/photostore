# Compatible clustering optimizations — 2026-11-05

Implemented locally against source baseline `ec52b30`; not deployed or timed on Azure. This implements the bounded first steps of the [revised algorithm](../clustering-artifact-algorithm.md), preserving the existing assignment/storage authority and wave receipts.

## Changes

- Wave assignment `_apply` enters the existing `source_change` context only for materialization. Tracked face/person clients retain operator admission, live observations, genuine ETags and coverage begin/finish reservations. For wave-owned libraries they no longer schedule independent maintenance before/after each mutation. The context resets on success and error; ordinary foreground operations and non-wave ownership retain scheduling.
- Frozen membership is fetched in ranges of up to 1,000 rows; inbox/manifest/face-receipt validation still uses groups of at most 20. Canonical ordering, source identity, receipt digests and the assignment runner's 128-face checkpoint limit are unchanged. The private input iterator accepts a bounded fetch size of 20–1,000 for comparison/fallback.
- Canonical journal fallback still reconstructs the applied prefix, but no longer checkpoints replay-only pages. It cannot repeatedly rewind the execution progress string or count replay alone as productive new work. Valid native/workspace resume continues to skip preparation/build/prefix replay through the existing implementation.
- Added `journal_open` phase timing and counters for resume hits/misses, newly resolved versus replayed faces, member fetches and member rows. A fresh run/no snapshot counts as a resume miss; a newly resolved face includes preserved/excluded/curated outcomes and is not necessarily a new person assignment. Optional snapshot failure and ownership/storage errors retain their existing behavior.

## Offline evidence

The existing audit reproduction uses the real matcher and `SourceTable` wrappers with in-memory storage, genuine fake ETags and synthetic faces matching one person. Scheduling is recorded rather than executed. It measures operation counts and output parity, not production latency.

| 128 incoming faces, tracked wave-owned clients | Before | After |
|---|---:|---:|
| Scheduling calls | 512 | **0** |
| Coverage transactions | 512 | 512 |
| Coverage reads | 513 | 513 |
| Admission reads | 768 | 768 |
| Equivalent assignment output | yes | yes |

Full before/after output: [call-count evidence](clustering-compatible-optimization-20261105.json). Runner: `docs/audits/clustering-speed-2026-11-05-reproduce.py`. Historical before output was captured before implementation; after output runs the updated code. Labels such as `tracked_current` identify the same runner mode in each snapshot.

The immutable-input regression compares the same 41 frozen members: member range fetches fall from **3 to 1**, with identical face snapshots and receipt validation groups `[20, 20, 1]`. These are member-list calls only, not all receipt/storage calls. The count bound is 1,000 member rows in memory, independent of the byte-bounded receipt cache; this is not a new total-RSS guarantee.

The recovery regression removes only an optional workspace snapshot after one durable face. Fallback replays that face without checkpointing it, then checkpoints newly resolved pages. A completed duplicate delivery creates no new progress checkpoints. Existing resume tests also verify a valid snapshot skips earlier native build/preparation and replay.

## Validation and limits

The broader run passed **412 tests** across journal/curation recovery, source batching, real wave consumers, exact publication receipts, continuations/health, automatic coverage/runtime integration, native checkpoints, activation/derivative evidence, metrics/diagnostics and wave runtime. Additional boundary tests exercise zero/one/exactly-1,000/1,001 member rows and invalid fetch bounds.

No coverage consolidation, authoritative artifact migration, page-wide speculative decisions or journal group commit was introduced. Those require the separate proofs described in the algorithm. No old immutable journal/header binding was edited, no wave source state was reset and no deployment or cloud mutation was performed.

A storage-backed before/after test is still needed to quantify throughput. Compare the same frozen inputs/baseline and image resources; record real WorkStore requests/queue sends, storage calls, journal open/save/restore durations, resume rate, new/replayed faces, transferred bytes and complete cluster elapsed. The earlier 2–5× throughput estimate is not a measured result of this patch.
