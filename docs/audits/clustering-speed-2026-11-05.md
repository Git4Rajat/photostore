# Clustering speed audit — 2026-11-05

Clustering is spending most of its elapsed time preparing, recording and materializing assignments. Improving FAISS search alone will barely change completion time. The largest actionable findings are **write amplification in automatic-index tracking**, **repeated prefix reconstruction on historical continuations**, and **per-face durability round trips**.

This audit traces source at `ec52b307620e6bdb01963f9baf5d77c29b17be2a`, re-examines retained November 2–3 Azure telemetry, and runs an offline experiment through the real matcher and source-table wrappers. It does not inspect today's deployment or run new Azure queries. Historical timing is not a measurement of the current image. No application code, cloud state, queues or configuration was changed.

## 1. What the 10-second measurement means

For wave `852ae63d0d12447cbe79c618e94798f8`, the retained phase records show:

| Measurement | Elapsed | Other evidence |
|---|---:|---|
| Whole cluster attempt | approximately 930 s | 1,902 faces; historical report records 74,659 storage calls |
| `assignment_persistence` | 550.640 s | 1,902 calls; 64,295 attributed storage calls |
| `snapshot_prepare` | 261.841 s | 1 call; 6,417 attributed storage calls |
| `journal_write` | 101.177 s | 26,253 calls, including snapshot records |
| `guard_check` | 44.421 s | 52,842 checks, 4,998 attributed storage calls |
| `curation_check` | 18.128 s | 1,902 calls |
| `matching` | **10.086 s** | 1,902 calls |
| `runtime_build` | 4.615 s | 1 call |
| `runtime_update` | 3.749 s | 1,902 calls |

**These are inclusive phases.** Journal and guard work is nested inside other phases. Do not sum this table or interpret all storage attributions as disjoint network requests. A phase timer measures elapsed duration; process CPU includes other threads. Missing summaries mean historical totals can be lower bounds.

Matching is approximately **1.08%** of this attempt. Eliminating it entirely would save approximately ten seconds; doubling its speed would save five seconds. Linear scaling of the matching measurement gives `10.086 × 10,000 / 1,902 = 53.03 seconds` for 10,000 faces under comparable conditions. It is neither an end-to-end SLA nor a demonstrated 10k benchmark.

The baseline library size, new/preserved/excluded face mix, person distribution, embedding compatibility groups and native index type matter. `_decide` includes early returns for preserved/rejected faces, so a phase call is not proof of an actual nearest-neighbor search. The base index uses exact flat search at small sizes and may use IVF-PQ above the configured threshold; runtime delta growth/rebuilds also change cost. Ten thousand photos is not ten thousand faces.

Evidence: [historical performance report](upload-wave-performance-2026-11-03.md), [extracted phase data](clustering-speed-2026-11-05-phases.json). Raw retained source: `.azure/upload-wave-review-20261103/phases.json` and its `.kql`.

## 2. Actual execution path

```text
wave_runtime.consume: cluster branch
  → run_cluster_release: claim execution + library ownership
  → run_frozen_assignment
      → open authoritative mounted JSONL journal, rebuild local journal index
      → restore validated workspace/native resume snapshot, if available
        OR load/capture frozen inputs + library baseline → build runtime
      → canonical face order, one decision at a time
          choose person using private FAISS + workspace
          persist decision before materialization
          witness live curation/source identity
          materialize person → membership → face completion stamp
          persist applied marker
          update private runtime/workspace
      → progress checkpoint at page boundary (up to 128 faces)
      → save resume snapshot and yield if slice budget elapsed
        OR publish compact assignment receipt
  → downstream index obligations / durable handoff
```

Active SQLite and FAISS state live on local temporary storage. The authoritative file journal and closed acceleration snapshots live on the checkpoint share. There is also a compatible Table-backed journal for older waves. The live cluster release requests the mounted path; a missing checkpoint configuration can select the Table path for an unmarked wave. Mode must be measured per wave.

Source anchors:

- `backend/wave_runtime.py:370` — live cluster dispatch; `:385` separately scopes index metadata preparation as source work.
- `backend/upload_wave_execution.py:690` — immutable receipt/input loading; `:755` — cluster release; `:777` — slice deadline.
- `backend/wave_assignment.py:142` — snapshot capture/reload; `:235` — decision; `:285` — curation witness; `:315` — materialization; `:421` — ordered loop.
- `backend/wave_assignment_files.py` — authoritative journal durability and local indexing.
- `backend/wave_assignment_resume.py` — optional workspace/native resume snapshots.

The legacy `clustering_wave.run_wave_assignment` harness and `FaissAssigner.batch(coalesce_writes=True)` are not the live frozen-wave materialization loop. Turning on legacy write coalescing does not automatically optimize `_apply`.

## 3. Finding A — source-table wrappers multiply per-face storage work

**Confirmed in current source and reproduced offline. Highest-priority persistence investigation.**

`app._get_live_faiss_assigner()` unwraps `_InvalidatingTableClient` once. Its underlying face/person clients remain `automatic_index_updates.SourceTable` wrappers, installed by `storage_utils.track_index_source_table`. Therefore the apparently simple `table.update_entity` inside `_apply` also executes automatic coverage tracking.

For each tracked face/person mutation, `SourceTable.write` performs admission checks, rereads the old source row via `mutation_targets`, reserves coverage before the write, finalizes coverage afterward and ordinarily calls scheduling before/after. These are real correctness mechanisms, but their frequency is per source mutation rather than per bounded assignment group.

The cluster branch in `wave_runtime.consume` does not establish `wave_policy.source_change`; neither `run_cluster_release` nor `run_frozen_assignment` establishes it. Vision processing and downstream index preparation do. The source wrapper's wave-owned behavior requires **both** the source context and wave ownership. As currently invoked, ordinary cluster writes therefore reach the scheduling branch as well as coverage reservation/finalization. Scheduling calls can coalesce; 512 calls do not imply 512 actual queue messages.

### Controlled reproduction

Ran the real `run_frozen_assignment` and `SourceTable` with real FAISS, synthetic 128-face input matching an existing person, genuine fake ETags, mounted journal on local scratch and in-memory Azure table fakes. Scheduling was recorded rather than executed. Fake transaction internals are suppressed from outer-call counts.

| Calls for 128 new assignments | Raw clients: control only | Tracked: current context | Tracked: source context |
|---|---:|---:|---:|
| Face reads | 128 | 256 | 256 |
| Person reads | 128 | 256 | 256 |
| Coverage reads | 0 | 513 | 513 |
| Coverage transactions | 0 | **512** | **512** |
| Coverage initial create | 0 | 1 | 1 |
| Admission reads | 0 | **768** | **768** |
| Scheduling invocations | 0 | **512** | **0** |
| Person + membership + face writes | 384 | 384 | 384 |

All three modes produced equivalent assignment output. The raw mode is a measurement control, **not a safe production bypass**. The source-context mode is also an experiment, not an implemented fix.

The tracked path adds 2,050 outer storage calls across these categories (including the first coverage-row create), before the real scheduling implementation's work. Wave ownership is a constant callback in the experiment, excluding production policy lookup cost. Snapshot queries and wave journal/lease storage are outside this comparison. These counts explain a mechanism; they do not assign every historical request to that mechanism.

Historical corroboration: the 1,902-face attempt attributed approximately 33.8 persistence storage calls/face. Later 128-face pages attributed 43.2–47.0 calls/face. The visible person/member/face writes alone cannot explain those totals. Current wrappers provide a concrete source of amplification, although precise historical attribution still requires matching the recorded image and tracing the schedule hook.

### Proposed changes and required proof

1. **Establish an explicit cluster wave-source scope**, after validating the wave ownership boundary. Verify it suppresses standalone maintenance scheduling while retaining ordinary foreground mutation behavior. Measure actual WorkStore requests, queue sends and dirty-row writes. A decorator alone leaves the coverage transactions intact.
2. **Design a bounded assignment coverage scope.** Reserve coverage once before a bounded page's source effects, collect affected people, and finalize only after all effects have a durable outcome. Prefer the existing coverage protocol with fewer begin/finish cycles over a global bypass. Preserve operator admission fencing and publication revision invalidation.
3. Reuse the already-read live entity/ETag within a narrow supported wrapper API where possible, eliminating redundant `mutation_targets` rereads. Only skip a read when the same source generation and conditional-write semantics are proven; keep old/new owner tracking complete.
4. Make scope failure explicit: source error, lost response, failed begin, failed finish and shutdown must leave visible retryable/fail-closed state. Existing reservations never expire automatically; do not create one unbounded wave reservation that can strand coverage after SIGKILL without a qualified recovery design.

Anchors: `backend/app.py:1571–1584`, `:6288–6325`; `backend/storage_utils.py:906`; `backend/automatic_index_updates.py:295`, `:337`, `:358`, `:513`, `:600`; `backend/wave_policy.py:31–43`. Source ranges are audit anchors, not generated line guarantees.

Artifacts: [reproduction script](clustering-speed-2026-11-05-reproduce.py), [recorded results](clustering-speed-2026-11-05-reproduction.json).

## 4. Finding B — historical continuation cost grew with the prefix

**Confirmed historical pathology; current source already contains a mitigation. Validate it before adding another implementation.**

Four consecutive retained attempts for wave `5c061254feaf4cd28a5ffc880e64c79b` show:

| Cumulative cursor | New `_apply` calls | Attempt | Snapshot preparation | Runtime build | Runtime updates |
|---:|---:|---:|---:|---:|---:|
| 4,608 | 128 | 172.29 s | 42.01 s | 32.56 s | 4,608 |
| 4,736 | 128 | 174.09 s | 41.73 s | 30.89 s | 4,736 |
| 4,864 | 128 | 172.63 s | 42.62 s | 31.22 s | 4,864 |
| 4,992 | 128 | 180.51 s | 42.13 s | 31.54 s | 4,992 |

For the last three attempts, cursor differences independently establish 128 new faces. This is approximately **0.71–0.74 new faces/second**, not 4,992 faces in three minutes. `faces_processed` is a cumulative cursor; dividing it by attempt time overstates useful throughput. Curation and checkpoint calls also replayed the prefix: 39 checkpoint writes in the last attempt, although only one new page was applied. Matching the 128 new faces took 0.67–0.73 seconds.

Preparation + native rebuild alone took approximately 73–75 seconds repeatedly. Prefix reconstruction, journal decoding, runtime updates and other work add more. The 120-second slice deadline starts before preparation/restore. Once startup consumes most of that budget, the code deliberately makes one new page durable before yielding, preventing starvation but allowing this poor throughput pattern. Increasing retries or replicas does not remove that work.

Commit `88b7281` already added `wave_assignment_resume.py` and resume tests. Current source saves the workspace and native runtime at a durable page boundary, then publishes a resume marker. A successful restore skips `prepare`, `runtime.build` and earlier `_replay` calls. Local qualification passed during this audit. The historical replay data predates proof of that behavior in production; this audit makes no claim that today's deployment still suffers the same full rebuild loop.

Remaining costs even with a hit:

- `FileAssignmentJournal.__init__` still reads/checks/indexes the **whole authoritative JSONL journal** into a fresh local SQLite database before `restore` runs.
- Every save backs up workspace SQLite, serializes native indexes/runtime SQLite, copies/checksums closed files to the share and publishes markers. Every restore copies/validates them back locally.
- Temporary directories and runtime objects are destroyed at each attempt. There is no live same-owner continuation cache in this loop.
- A missing/stale/damaged acceleration snapshot falls back to authoritative replay. A share/ownership I/O failure propagates; it must not become an unsafe cache hit or silently recapture partially materialized state.

Next measurements: resume hit/miss reason, journal bytes/records decoded, workspace/native bytes copied, snapshot save/restore durations, replayed versus newly applied faces and progress per slice. If full journal indexing dominates, consider a validated journal-index checkpoint or immutable sealed segments plus a checked tail. Preserve digest coverage, torn-tail handling and fail-closed recovery. A cursor alone is insufficient.

Evidence: [extracted replay records](clustering-speed-2026-11-05-cluster-slice-replay-costs.json), raw `.azure/upload-wave-review-20261103/cluster-slice-replay-costs.{json,kql}`, current resume tests in `backend/tests/test_wave_assignment_files.py`.

## 5. Finding C — fsync is real, but the old batching proposal is incomplete

**Current source still flushes each new non-preparation journal record. A redesign is required for safe batching.**

`FileAssignmentJournal.put` invokes `_flush` for each decision and applied marker; `_flush` synchronizes the share and commits the local journal index. Preparation buffers its snapshot rows until the manifest. The October 30 audit measured 1,972 syncs taking 22.9 seconds for a 985-face wave. At that historical latency, 20,000 syncs would cost roughly 232 seconds for 10k faces. This is a latency projection, not a fresh measurement or a whole-stage saving.

The [old fsync benchmark](../benchmarks/cluster-journal-fsync-20261030.md) demonstrates fewer flushes and equal final record maps. It does **not** execute materialization, fresh curation, real dependent decisions or crash recovery through a proposed batching algorithm.

There is an additional current-code obstacle: `_curation_override` explicitly rejects an override when `journal.decision(position + 1)` exists. Preparing a whole page of durable decisions before applying its first face creates exactly those dependent decisions. A foreground curation edit to that first face can therefore invalidate the proposed page, or fail the existing check. Simply flushing decisions per page and keeping the existing application loop is not a qualified optimization.

Candidate designs, in increasing complexity:

1. Batch **applied acknowledgements** while keeping decisions durable individually, if the explicit flush cadence actually reduces syncs. Merely delaying an applied marker until the next decision flush may still cost nearly one sync/face. Demonstrate the count and replay semantics before claiming a saving.
2. Introduce a proper page plan/commit protocol with explicit curation-conflict handling, speculative runtime rollback/rebuild, and invalidation of dependent unmaterialized decisions. Do not append a conflicting immutable decision under the same key.
3. Use another durable append medium/protocol only after measuring total latency and preserving ownership/recovery semantics. Ephemeral-only logs cannot replace the durable authority.

Acceptance must cover a curation edit **after page planning but before application**, source replacement, lost person/member/face replies, torn decision/applied writes and crashes after each prerequisite. Final-file parity on the happy path is insufficient.

## 6. Finding D — growing person payloads and serial materialization

**Confirmed source behavior; workload-dependent performance impact needs measurement.**

Every assigned face serially reads/updates its target person, creates membership, then conditionally updates its live face. The face decision stamp is last and proves prerequisites landed. Curation witness and `_apply` already share the same face read and genuine ETag: do not propose that existing optimization as new work. Embedding fallback still reads `photofaceembeddings` when the live face lacks an inline vector.

`FaissAssigner._membership` parses and rewrites the full `person.faceIds` JSON for each added face. `_decide` also embeds that expanding person payload in its durable decision. If k faces accumulate on one person, cumulative membership serialization can grow quadratically with k, despite a separate membership table. The 60 KiB legacy membership budget can reject further additions; the decision journal's payload limit may also bind. This audit has not established that either limit caused a historical failure.

Measure person-row and journal bytes against faces/person. A longer-term design should make membership rows the scalable authority and keep bounded person summaries, with an explicit compatibility/migration plan for all readers, deletes, merges and curation flows. This is more than removing a JSON field.

Batching can be designed **within one table/partition**, but person, face and membership are separate table clients. Membership uses the person ID as partition; face/person rows use the library. The older audit's suggestion to atomically combine person + face because their PartitionKeys match is invalid in this schema. Azure transactions require the **same table and partition**, support at most 100 entities/4 MiB, and permit each entity only once. [Microsoft transaction requirements](https://learn.microsoft.com/en-us/rest/api/storageservices/performing-entity-group-transactions).

A bounded page materializer could group person writes, memberships per person and final face writes in separate phases, but must handle hot-person coalescing, fresh ETags, partial commits and dependent curation. A 128-face page is not automatically a valid 100-operation transaction. Preserve every face's prerequisites-before-stamp rule and authoritative journal recovery; do not enable blind parallel face assignment.

## 7. Finding E — snapshot preparation is partly proportional to library size

The wave input is not the entire cost. `WaveAssignmentJournal.prepare` loads immutable wave receipts, probes deterministic person IDs for recovery, scans existing eligible owned faces/embeddings, loads referenced people and journals that frozen baseline. A small incoming wave against a large library can still have substantial preparation cost.

Existing improvements are real: receipt batching with adaptive reads, cold face/embedding streaming, person workspace reuse and mounted snapshot buffering. The older conclusion that all remaining preparation cost is inherent is too strong: reloading/rebuilding the same frozen baseline on every slice was avoidable and current resume snapshots address it.

Current fresh-capture opportunities need profiling: member pages remain 20 here; person ID probes use bounded key batches; candidate capture flushes every 14 rows; broad library scans recur for a fresh wave. Separate immutable receipt fetch time, baseline scan bytes/rows, people lookup time, local ordering/hashing and journal publication. Consider larger range-fetch chunks, bounded look-ahead and version-qualified library snapshots only where measurements justify them. Changing wave fetch size must not change canonical order or checkpoint granularity.

## 8. Execution order and acceptance gates

| Order | Work | What must be demonstrated |
|---|---|---|
| 1 | Baseline the actual current image and resume mode | Exact image/config identity; new/replayed counters; no native rebuild/full prefix replay on a valid resume; distinguish restore cost from application cost |
| 2 | Scope cluster writes as wave source work | No standalone scheduling from those writes; foreground edits still tracked; assignments/receipts unchanged; coverage reservations retained |
| 3 | Consolidate coverage tracking at bounded durable assignment boundaries | Fewer coverage/admission/dirty/schedule operations per new face; no false coverage certification, lost dirty owners or orphaned invisible writer |
| 4 | Reduce journal open/save/restore overhead | Bounded new-page cost as cursor grows; digest/generation validation; recover correctly with missing optional acceleration state |
| 5 | Qualify journal group commit | Measured sync reduction; curation-dependent decisions handled; crash/lost-reply parity at every boundary |
| 6 | Optimize live materialization and person schema | Reduced reads/writes/bytes with same ETags and prerequisite ordering; hot-person/deletion/merge parity |
| 7 | Tune FAISS only if new profiles justify it | Equivalent assignment quality/policy; total time improves, including native build/rebuild costs |

Steps 2–3 target a previously missed mechanism. Step 1 prevents benchmarking historical bugs already mitigated in current code. No replica-count increase is required to start this work; the same-library canonical assignment and library lease serialize this path.

### Benchmark matrix

Use identical frozen input and baseline for each comparison. Control CPU/RAM, image/config, storage region, journal mode, schema, compatibility groups and candidate distribution. Run repeated trials (at least three) and report medians plus worst trial; separate cold start, first capture, resumed execution and full wave completion.

| Dimension | Cases |
|---|---|
| Incoming faces | 0, 20, 1,902, 10,000; 16,000 for continuation/rebuild stress |
| Existing baseline | empty, 10k, 100k faces; record persons and actual index type |
| Person distribution | mostly existing people; mostly new people; concentrated hot person up to schema limit; distributed groups |
| Face disposition | assigned, preserved, excluded, invalid vector; record actual searches separately |
| Continuations | uninterrupted control; normal 120 s slices; restart after each new page; snapshot miss/corruption |
| Concurrent edits | rename, reject/reassign face, delete person/face, source generation replacement |
| Failure boundaries | each prerequisite write, lost reply, journal append/flush, snapshot generation/pointer/marker, final handoff, SIGTERM |

Primary metrics: **newly durably applied faces/second**, complete cluster elapsed, storage calls and transferred bytes per new face. Secondary: replay ratio, resume hit rate and miss reasons, capture/open/save/restore/native build costs, sync count/latency, process CPU, RSS, local/share peak disk, coverage reservations left active and actual maintenance queue sends. Split fresh assignments from preserved/excluded work. Durable no-work waves should not initialize a matcher unnecessarily.

Do not sum inclusive phases. Reconcile attempt IDs with durable progress transitions; missing end summaries remain explicit unknowns. Report queue/predecessor delay separately from active stage time. A yielded slice's `success` outcome is not a completed cluster stage.

### Performance ambition

Use roughly **53 seconds of matching for 10k faces** only as a reference extrapolation. A useful initial engineering gate is **at least 2× higher useful throughput** on the same storage-backed 10k case with unchanged results and recovery behavior. Establish the current baseline before setting an absolute target.

For planning, a hypothetical budget of 60 s preparation, 53 s matching and 300 s total per-face persistence/journal work gives approximately **413 s (6m53s)** before measured continuation/handoff overhead. That requires approximately 30 ms/face for persistence plus journal work, versus approximately 290 ms/face for persistence alone in the 1,902-face sample. It is an aggressive design budget requiring fewer round trips, **not a forecast or claim already achieved**. Finishing the whole stage in 100 seconds would leave under 47 seconds for all other work, which the current per-face storage protocol does not support.

## 9. Verification performed and limits

- Offline wrapper experiment: three modes, 128 new faces each, equivalent assignment output. Saved call counts and reproducible runner accompany this audit. Real scheduling, network latency and production wave-policy lookup are excluded; no cloud credentials are needed.
- Existing focused qualification: **186 passed** across `test_wave_assignment_journal.py`, `test_wave_assignment_files.py`, `test_automatic_index_updates.py`, `test_clustering_checkpoint.py` and `test_wave_continuation_safety.py`.
- Those tests verify the current implementation, including resume/recovery boundaries. They do not qualify the proposed batching or coverage-scope design, which has not been implemented.
- Retained telemetry was extracted without modifying source data. Today's deployment, resume hit rate, large-library latency, wrapper schedule-hook contribution and SMB costs remain unmeasured in this audit.
- Older audit statements that preparation is entirely inherent, page fsync batching is already proven safe, and person/face writes fit one transaction are superseded by the findings above. Historical documents are retained unchanged for provenance.

Reproduction commands, from repository root:

```bash
env -u AZURE_STORAGE_CONNECTION_STRING -u AzureWebJobsStorage \
  STORAGE_ACCOUNT_NAME='' AZURE_STORAGE_ACCOUNT_NAME='' \
  .venv-photostore-tools/bin/python \
  docs/audits/clustering-speed-2026-11-05-reproduce.py

env -u AZURE_STORAGE_CONNECTION_STRING -u AzureWebJobsStorage \
  STORAGE_ACCOUNT_NAME='' AZURE_STORAGE_ACCOUNT_NAME='' \
  .venv-photostore-tools/bin/python -m pytest -q \
  backend/tests/test_wave_assignment_journal.py \
  backend/tests/test_wave_assignment_files.py \
  backend/tests/test_automatic_index_updates.py \
  backend/tests/test_clustering_checkpoint.py \
  backend/tests/test_wave_continuation_safety.py --disable-warnings --tb=short
```

App import emits an expected storage-initialization error with the storage account explicitly unset; all experiment storage is subsequently supplied by in-memory fakes. Local wall time is deliberately not presented as Azure throughput.
