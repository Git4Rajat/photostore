# Clustering post-rollout throughput — 2026-11-05

The user's 500-file upload did not show a material clustering-throughput gain. Read-only live telemetry confirms the deployed patch is active and its input/resume changes work, but the dominant per-face persistence and baseline preparation costs remain. The earlier 2–5× optimization estimate was not justified for the implemented subset and is withdrawn as a prediction for commit `767ada3`.

## Deployment and run

Checked live Container Apps state and the linked Log Analytics workspace after the report. Cluster is healthy (`Succeeded` / `Running`) on revision `forenklaprep1-dev-cluster--0000003`, image `ghcr.io/git4rajat/photostore-backend:767ada3a0ed9790f8c0341c34874d35b369fa7ae`, with 2 CPUs / 4 GiB and configured local-work/checkpoint mounts. No deployment, queue or source state was changed.

Wave `43f5e0dcbfa04e4dbd0c31129e93f6aa` fetched **500 member records** and resolved **492 face records**. Two cluster attempt summaries total **234.17 seconds (3m54s)**, approximately **2.10 face records/second**, and **14,916 attributed storage calls**. The first attempt resolved 256 records in 157.71 seconds; the resumed attempt resolved another 236 in 76.46 seconds and transitioned to Index. These durations are recorded execution elapsed, not upload-to-complete or CPU time.

The new counters confirm:

- One member-range fetch for all 500 members.
- One valid resume hit on continuation.
- Zero replayed faces across both attempts.
- 987 journal fsyncs across the attempts.

`cluster_faces_newly_resolved` includes preserved/excluded/curated outcomes. It must not be read as 492 newly assigned faces or new people. The counters are sufficient to distinguish journal progress from replay, not to equate image/face workloads.

## Remaining measured costs

| Cluster phase across both attempts | Recorded elapsed | Evidence |
|---|---:|---|
| Assignment persistence | **127.55 s** | 11,988 attributed storage calls; 113.28 s attributed storage-send time |
| Frozen snapshot preparation | **65.28 s** | 1,719 attributed storage calls; 30.37 s in other-table reads, 19.62 s in job reads |
| Journal writes | 29.04 s | Includes **13.30 s** of fsync time |
| Curation checks | 5.32 s | Existing live curation observations remain |
| Journal opening | 4.09 s | Both attempts combined |
| Resume restoration | 2.63 s | Both attempts combined |
| Actual matching phase | **2.24 s** | No attributed remote storage calls |
| Initial native runtime build | 1.73 s | One build |
| Resume save | 1.69 s | One saved continuation snapshot |

Phases are inclusive and nested: do not sum them into a second total or treat their storage counts as disjoint. Assignment persistence still spends most of its elapsed time waiting on storage operations. Removing the matching phase entirely would save under 1% of measured cluster execution. Removing all measured fsync time would save about 5.7%, before replacement-protocol overhead; it cannot produce a multi-fold improvement by itself.

Persistence attribution includes 5,508 job reads (46.10 s), 2,592 other-table reads (22.38 s), 2,592 other-table writes (31.43 s), and 432 embedding reads (5.12 s). These inclusive attributions motivate tracing coverage/admission/ownership operations rather than optimizing FAISS. They are not a complete exclusive call-by-call allocation or proof every call is redundant.

## Comparison limits and correction

An earlier run on the preceding revision resolved 774 face records in 381.45 seconds, approximately **2.03 face records/second**, with 37,348 attributed storage calls. The new run's throughput is in the same range. The workloads and existing-library baselines differ; this is not a controlled before/after benchmark and does not establish a precise percentage improvement.

The patch removed standalone scheduling, reduced member-list round trips and avoided replay-only progress writes. It deliberately left per-mutation coverage reservations and person/member/face persistence intact. Valid native resume was already implemented before this patch; the new counters verify its use rather than proving it is a newly delivered speedup. The offline 512 → 0 scheduling-call result never demonstrated end-to-end latency improvement.

For this 500-member run, member listing is already one request, there is no face-prefix replay, and matching is small. Those observations explain why the changes did not deliver the previously estimated gain: they did not remove the dominant work.

## Next performance work

1. Trace and prototype the bounded coverage/source-observation scope specified as Gate B in the compatible algorithm. Measure real admission, policy, coverage-control, reservation and scheduling operations separately. Keep genuine live ETags, old/new-owner coverage, operator fences, unknown-commit handling and fail-closed reservations. Do not simply unwrap tracked clients.
2. Break down the 65-second fresh snapshot preparation, especially existing-person lookups and baseline capture. Benchmark query shape, bounded concurrency and safe immutable reuse on the same baseline before changing it. Larger member pages do not eliminate those scattered reads.
3. Require a repeatable frozen-input/baseline comparison for the next patch, with complete stage elapsed and total storage calls as acceptance metrics. A helper-call reduction or test pass alone does not justify a throughput forecast.

No replacement throughput promise is made from these observations. A future authority migration remains a separate project with different data/concurrency compatibility requirements.

Raw read-only evidence is retained privately under `.azure/clustering-optimization-followup-20261105/{summaries,phases,wave-comparison}.json`. Queries selected recent `cluster_wave_summary` / `cluster_wave_phase` events and stage summaries from the environment's current workspace; per-attempt IDs were retained in summaries and phase totals filtered to the exact wave. No application code changed during this follow-up.

## Authorized implementation follow-up

The subsequent [bounded coverage and baseline-read implementation](../benchmarks/clustering-page-coverage-20261105.md) is complete locally, with 348 passing targeted tests and exact offline assignment/manifest parity. The identical 500-face comparison reduces counted calls from 9,540 to 3,560, including coverage transactions from 2,000 to 8. A read-only Azure lookup experiment supports four concurrent bounded queries over per-key point reads. These measurements have explicit scope limits and do not establish a whole-wave throughput gain. No follow-up deployment has been performed.
