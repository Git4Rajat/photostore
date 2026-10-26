# Vision and clustering efficiency — 2026-10-26

## Evidence scope

Read-only audit of wave `51ad8be70977431d9999cffacb4c1b01`, using Log Analytics
console records between 17:04 and 19:40 UTC. This wave crossed the deployment of
`72a97d5`, which unblocked its last 20 members. Separate productive work from the
old inspection-error loop; do not use the whole elapsed run as a throughput
benchmark. Source code includes the later local receipt fix `8336c7d`; that fix
was not part of this run. No performance implementation changes in this audit.

## Vision

The initial worker processed 991 images. Its 13 productive queue-message scopes
recorded 135,932 SDK calls, 1,265.71 seconds of scoped elapsed time and 1,157.31
seconds of aggregate SDK time. Job-table GETs alone contributed 75,424 calls
and 579.07 seconds. SDK durations can overlap across concurrent workers.

Per-image phase elapsed averages for that worker:

| Phase | Average ms | Aggregate seconds |
| --- | ---: | ---: |
| Face inference | 403.7 | 400.11 |
| Face persistence | 247.3 | 245.04 |
| CLIP inference | 188.4 | 186.72 |
| OCR | 113.3 | 112.24 |
| Semantic text refresh | 105.0 | 104.05 |
| Lease/policy check inside metadata commit | 15.7 | 15.52 |
| Embedding write | 14.2 | 14.06 |
| Metadata CAS | 12.9 | 12.82 |
| Dirty marking | 8.2 | 8.16 |
| Metadata merge | 0.4 | 0.39 |
| Completion validation inside metadata commit | 0.3 | 0.32 |

These phase aggregates are not an additive wall-time decomposition. The tiny
completion-validation span does not include `ArtifactReceipts.capture`, prepared
artifact promotion, source-lease acquisition, or all completion-relay work.

61 later pre-fix message scopes performed another 304,683 SDK calls, including
296,049 job-table GETs, without processing new images. This is failed coordination,
not model inference; the inspection-loop fix addresses this specific failure.

### Priorities

1. Consolidate library-policy and source-fence reads around source/batch commit
   boundaries. `ResultFence.check` rereads policy and metadata, while wave-level
   `renew` rereads policy, wave, dispatch and execution ownership under a shared
   renewal lock. Do not trade away conditional write ownership checks.
2. Batch face persistence and receipt capture; reuse immutable face/vector data
   already qualified in the current attempt instead of rereading it per effect.
3. Avoid repeated derivative byte verification within the same attempt.
   Promotion reads prepared bytes, publication reads back immutable output, and
   capture verifies that output again. Retain independent verification when
   resuming or accepting externally prepared content.
4. Benchmark semantic-text and image-model batching only after coordination
   changes. Thread concurrency two is not tensor batching. Metadata merge/CAS
   are much smaller optimization targets than face persistence and policy reads.
5. Treat cover input reuse as a lower-priority experiment: 962 original-image
   reads were attributed to face covers. Any reuse must preserve crop resolution
   and quality, not silently replace original crops with inadequate previews.

## Clustering

The successful stage processed 1,041 faces against 869 baseline vectors and 845
people. Its correlated wave summary reported:

- Elapsed: 1,029.139 seconds (17m 9s).
- Process CPU: 280.399 seconds; includes native threads and instrumentation.
- SDK calls: 129,852; SDK time: 920.272 seconds.
- Matching: 4.223 seconds, zero SDK calls.
- Local runtime updates: 1.959 seconds, zero SDK calls.
- Snapshot preparation: 514.664 seconds, 64,794 SDK calls.
- Assignment persistence: 160.665 seconds, 19,994 SDK calls.
- Curation checks: 60.263 seconds, 8,328 SDK calls.
- Runtime build: 33.160 seconds, 4,345 SDK calls.
- Checkpoints: 0.139 seconds, 18 SDK calls.
- Lock/lease acquisition: 0.073 seconds.

Four disjoint control-read phases total 102,396 calls (78.9% of the summary's
SDK calls) and 731.335 elapsed seconds: policy 20,479; wave 20,479; dispatch
20,479; ownership 40,959. Other phases contain these checks, so their times
must not be added to the control-read total.

The 4,840 journal writes consumed 466.258 seconds and 58,116 SDK calls. Their
count matches 2,755 snapshot rows (inputs + baseline + people), 2,082 per-face
decision/applied rows, and capture/manifest/completion records. Roughly twelve
remote calls accompany each journal write. This is recovery/fencing overhead,
not FAISS matching. `runtime_build` includes five remote checks for every one
of its 869 baseline candidates; its 33 seconds cannot be called pure FAISS cost.

### Priorities

1. Keep the frozen snapshot and decision journal on mounted local storage,
   consistent with the accepted restart-from-scratch recovery tradeoff.
2. Remove storage access from local snapshot iteration/runtime construction.
3. Persist person/membership/face changes through bounded batches, with ownership
   and revision checks at commit boundaries and protection for user curation.
4. Keep a periodic stage heartbeat and publish only after all result batches
   succeed. Do not optimize FAISS, lock contention, or checkpoint frequency first.

## Remaining measurement gaps

- Add a Vision wave/source summary with new processing, qualified reuse, pending,
  explicit failure and inspection-failure counts. Existing legacy
  `productive_completed` stays zero even while wave-source commits succeed.
- Separate outer queue-task occupancy from inner image-worker occupancy. Current
  slot utilization counts outer tasks against concurrency two and is not a
  measurement of the two source workers' utilization.
- Measure source claim/release, derivative promotion, artifact capture/verification,
  completion relay, eligibility reads, and time waiting for the renewal lock.
- Attribute SDK calls to inner tasks or label them shared-scope (as currently
  done). Per-image shared-scope counter deltas must not be summed as unique calls.
- Attribute native CPU to model work carefully: calling-thread CPU excludes native
  helper threads; process CPU can include overlapping image work.
- Fix media-download purpose attribution: all 991 initial inference thumbnail
  downloads were labeled `unattributed`.
- Split expected point-read misses from transient service errors. An SDK error
  counter is not itself a failed-image counter.

For the next benchmark, compare the same inputs and fixed starting library
snapshot. Report unique source completions, failures, stage wall/CPU time,
SDK calls per source/face, and output parity. More replicas are not evidence
of improved efficiency; establish reduced coordination cost first.
