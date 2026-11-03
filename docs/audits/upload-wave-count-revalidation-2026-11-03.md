# Upload wave count revalidation — November 3, 2026

Read-only Azure Table and Log Analytics investigation, approximately 14:28–14:33 UTC. Times below are Stockholm (UTC+1), on November 2. No cloud rows, queues, application configuration, or application code were changed.

## Conclusion

Wave memberships are not new-photo counts. The five large waves contain 41,389 memberships but only 32,025 distinct photo IDs and 41,173 distinct photo/revision pairs. The last three waves contain 21,202 memberships but only 11,850 distinct photo IDs (20,998 distinct photo/revision pairs). These photo IDs do not establish when content was first uploaded.

Duplicate-only upload sessions generated substantial new wave work. Earlier reporting should have distinguished new uploaded content, duplicate intake, wave membership, and processing attempts. Capacity estimates based on wave memberships must not be interpreted as throughput of new unique photos.

## Exhaustive Table counts

SDK pagination was exhausted for all 12 wave rows, 17 sessions, 150,360 intake item rows, 57,069 wave evidence rows, and 58,622 metadata rows. Membership counters match actual member rows for all waves.

| Wave prefix | Created | Members | Distinct photos | Photos overlapping preceding waves* |
|---|---|---:|---:|---:|
| 6d61cb0a | 14:47 | 10,207 | 10,207 | 0 |
| 88397a60 | 19:26 | 9,980 | 9,980 | 1,498 |
| 5c061254 | 20:16 | 10,035 | 10,035 | 1,351 |
| 01937bc4 | 21:08 | 8,661 | 8,661 | 6,929 |
| 04b165a3 | 22:38 | 2,506 | 2,506 | 2,423 |

*Against all earlier persisted waves, including older completed waves; not a count of all previously uploaded content. Some content existed outside the earlier wave member sets.

Wave 01937bc4 shares 6,929 photos with 5c061254, but only 204 identical photo/revision pairs: 6,725 shared photos have different revisions. Wave 04b165a3 shares 2,423 photos with each of those two waves, with zero identical photo/revision pairs.

Metadata has 58,622 rows but only 43,049 populated distinct anonymous photo IDs and 43,048 populated distinct hashes. Multiple metadata filenames can refer to one photo. 6,730 photo IDs have multiple processing revisions across current metadata rows. Earliest surviving uploadDate per photo falls on November 2 for 29,284 IDs; uploadDate is not a sufficient immutable first-upload audit, so this does not prove 29,284 new content uploads that day.

## Independent browser and server telemetry

Browser summaries:

- 14:47 batch: 50,474 selected, 9,909 uploaded, 6,383 duplicates at its recorded end. Selection size is not completed-upload count. A resumed run reports another 12 uploads; another resume has no end summary.
- 10:59 batch: 1,871 uploaded, 27 duplicates.
- 21:53 run: 0 uploaded, 1,520 duplicates.
- 22:38 run: 0 uploaded, 2,508 duplicates; associated wave nevertheless has 2,506 members, all marked needsProjection=true and accepted.

Server upload_finalized telemetry, deduplicated exactly by hashed library/filename within each batch (not approximate dcount):

| Batch start | Finalization events | Duplicate events | Distinct nonduplicate filenames |
|---|---:|---:|---:|
| 19:26 | 12,445 | 3,950 | 8,495 |
| 20:16 | 13,705 | 5,022 | 8,683 |
| 21:08 | 11,301 | 11,301 | 0 |
| 22:38 | 3,166 | 3,166 | 0 |

Retries, aliases, incomplete browser summaries, and current-row timestamps prevent certifying an exact whole-day unique-new-content count from these aggregates. The evidence confirms inflated/repeated wave work; it does not establish that the whole day had at most 10,000 unique new photos. The 19:26 and 20:16 runs also have substantial server nonduplicate classifications, which need canonical content reconciliation before being called new photos.

## Mechanism supported by code and persisted data

1. backend/routes/upload_wave_live.py:124–126 binds every successful non-replay finalization as accepted, without preserving its duplicate result. These lines date to November 2, before the affected runs.
2. backend/storage_utils.py:11654 detects duplicates, but finalization continues to metadata writes. At 11743 onward it resets processing statuses, clears leases, and resets retry counters even for duplicates.
3. Revision allocation at 11689–11702 compares the destination filename's metadata identity and allocates a revision if absent or changed. Metadata aliases can therefore carry separate revisions for the same photo ID. Observed cross-wave revisions demonstrate that the repeated work is not solely a display double count.
4. backend/upload_waves.py:342 and 391–407 admit both accepted and duplicate outcomes as members. Deduplication is per wave and photo/revision, not globally per photo. Accepted outcomes set needsProjection=true.

The accepted classification and status-reset paths explain why duplicate finalizations can become processing work. Pinning every repeated revision to an individual alias-creation event would require more per-file history; do not infer all overlap was caused by one specific retry or deployment.

## Corrective direction

Preserve authoritative duplicate outcomes in admission. For unchanged content, bind the canonical existing photo/revision and preserve completed step statuses, receipts, leases, and retry state. Allocate a fresh generation only for a real source change or explicit reprocessing. Treat filename aliases as references to the same canonical source. Report distinct new content, duplicate intake, reprocessing generations, wave memberships, and source attempts separately. Existing waves require receipt-aware reconciliation; do not blindly delete duplicate-looking members or mark them complete.

Evidence is retained locally under .azure/upload-wave-review-20261103: revalidated-counts-summary.json, revalidated-counts-raw.json, revalidate-browser-batches.json/.kql, and revalidate-server-finalizations.json/.kql. Raw identifiers and filenames are omitted from this report.

## Follow-up: observed Vision behavior, approximately 14:39–14:45 UTC

Live app check: Running, provisioning Succeeded, ready revision 0000066. Read-only investigation; application files were being edited concurrently by another actor and were not changed by this investigation.

Per-member telemetry was joined to persisted member row identities using the exact member_id hashing algorithm. This distinction materially narrows the earlier explanation: duplicates do not all run inference.

- Wave 88397a60: all 1,498 members marked needsProjection=false were inspected and receipt-qualified, with zero source attempts, zero processing-step calls, and zero face-detection time. The 8,482 needsProjection=true members included 8,476 processing-step executions.
- Wave 5c061254: 623 of its 1,351 needsProjection=false members had been inspected; all 623 receipt-qualified with zero source attempts, step calls, or face-detection time. These are partial-progress counts.
- Within wave 5c061254's own upload batch (4bdc541081f14d29b7a9449a9aebef21), 905 filenames flagged duplicate, with no nonduplicate finalization event for that filename in the same batch, appeared in subsequent Vision image logs. Of these, 901 had a source attempt and processing-step call; aggregate face detection was 112,430.897 ms. This confirms actual processing/model work after duplicate detection, beyond receipt validation. It does not prove every file had previously completed inference elsewhere.
- Other filenames with both successful nonduplicate and duplicate finalization events were separated: a duplicate flag can be a retry of the original successful finalization, so joining duplicate events to later processing alone is insufficient to prove redundant inference.
- Wave 01937bc4 (8,661 members) and 04b165a3 (2,506 members) remained waiting_predecessor, with no vision_image_summary or vision_wave_attempt_summary events in the queried interval. The duplicate-only wave backlog is admitted work, not evidence those two waves have already rerun inference.

The observed behavior is therefore mixed: properly recorded duplicates reuse existing receipts; some duplicate finalizations admitted as accepted perform real processing; the two later duplicate-only waves have not yet run. Earlier wording that implied those two waves had already consumed Vision inference capacity was too strong.

Additional evidence: duplicate-wave-vision-status.json/.kql, duplicate-member-results-detailed.json/.kql, duplicate-member-reconciliation.json, duplicate-only-file-inference.json/.kql, duplicate-inference-examples.json/.kql. Queries use exact sets, not approximate distinct counts. Per-image logs and wave counters are snapshots during ongoing processing and may have different cutoffs.
