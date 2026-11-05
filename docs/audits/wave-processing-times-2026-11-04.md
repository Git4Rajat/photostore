**Processing time versus wall time — all waves**

Snapshot: Nov 04 00:04 Stockholm (2026). Includes all 61 persisted waves in both libraries.

Processing means recorded stage execution elapsed time, including model work, coordination and storage I/O. It is not CPU time. Parallel photo-worker durations are not added to stage totals. Wall time runs from upload intake closure to completion, or to this snapshot for unfinished waves. It includes dependency waits, queue delays, retries, and interruptions. Upload-start-to-end time is shown separately.

**Coverage:** “≥” means a lower bound: one or more attempts ended without a summary, a required stage has no timing summary, or the stage is still in progress. “Unavailable” is missing telemetry, not zero work. “Bypassed” is a durable no-work completion. Older index stages were not instrumented, and forcibly terminated attempts may never emit a summary. Cancelled waves are marked and do not represent successful completions.

Cancelled-wave wall spans end at the recorded cancellation transition where available. Stage wall spans start at the previous stage completion; predecessor publication waits are included. For unfinished waves the total processing value is recorded work to date, not an ETA.

**hack4heart — totals**

| Created (Stockholm) | Wave | Members | State | Recorded processing | Wall after intake closed | Upload start → end |
|---|---|---:|---|---:|---:|---:|
| Oct 29 14:33 | `91ecc876` | 1,001 | complete | ≥ 9m 55s | 17m 42s | 22m 23s |
| Nov 01 11:09 | `03497b11` | 90 | complete | 1m 33s | 23m 46s | 24m 42s |
| Nov 01 11:10 | `f28524d6` | 1,266 | complete | 20m 19s | 20m 58s | 32m 19s |
| Nov 01 12:10 | `33753020` | 2,738 | complete | ≥ 43m 50s | 50m 32s | 1h 8m 53s |
| Nov 01 21:55 | `b558b517` | 5,181 | complete | ≥ 2h 5m 41s | 13h 36m 1s | 14h 37m 15s |
| Nov 01 23:32 | `36c133ee` | 3,489 | complete | ≥ 1h 3m 28s | 13h 19m 36s | 13h 48m 42s |
| Nov 02 10:59 | `852ae63d` | 1,898 | complete | 55m 34s | 5h 13m 18s | 5h 26m 27s |
| Nov 02 14:47 | `6d61cb0a` | 10,207 | complete | ≥ 4h 24m 10s | 22h 45m 35s | 23h 51m 17s |
| Nov 02 19:26 | `88397a60` | 9,980 | complete | ≥ 2h 57m 45s | 25h 30m 9s | 26h 27m 21s |
| Nov 02 20:16 | `5c061254` | 10,035 | ready | ≥ 4h 38m 14s | 26h 41m 27s ongoing | 27h 48m 37s |
| Nov 02 21:08 | `01937bc4` | 8,661 | ready | ≥ 3h 19m 7s | 25h 58m 34s ongoing | 26h 56m 15s |
| Nov 02 22:38 | `04b165a3` | 2,506 | ready | ≥ 47m 33s | 25h 12m 27s ongoing | 25h 26m 17s |

**hack4heart — stage detail**

Each stage cell is **recorded processing / wall time**.

| Wave | Members | Vision | Clustering | Indexing |
|---|---:|---|---|---|
| `91ecc876` | 1,001 | 8m 8s / 9m 38s | 1m 48s / 2m 19s | unavailable / 5m 45s |
| `03497b11` | 90 | 48s / 49s | 14s / 22m 1s | 30s / 55s |
| `f28524d6` | 1,266 | 15m 41s / 15m 43s | 2m 5s / 2m 12s | 2m 33s / 3m 3s |
| `33753020` | 2,738 | ≥ 31m 6s / 36m 6s | 4m 16s / 5m 14s | 8m 28s / 9m 12s |
| `b558b517` | 5,181 | ≥ 1h 3m 59s / 12h 33m 33s | 35m 49s / 36m 32s | 25m 53s / 25m 55s |
| `36c133ee` | 3,489 | 44m 50s / 12h 13m 46s | ≥ 1m 48s / 48m 45s | 16m 50s / 17m 4s |
| `852ae63d` | 1,898 | 27m 5s / 4h 44m 17s | 15m 30s / 16m 1s | 12m 59s / 13m 0s |
| `6d61cb0a` | 10,207 | ≥ 2h 25m 30s / 19h 55m 40s | 1h 28m 50s / 1h 29m 24s | ≥ 29m 51s / 1h 20m 30s |
| `88397a60` | 9,980 | ≥ 2h 20m 41s / 17h 51m 39s | ≥ 12m 38s / 3h 5m 26s | ≥ 24m 26s / 4h 33m 4s |
| `5c061254` | 10,035 | ≥ 3h 3m 44s / 20h 19m 42s | ≥ 1h 34m 29s / 6h 21m 44s ongoing | unavailable / — |
| `01937bc4` | 8,661 | ≥ 3h 19m 7s / 23h 20m 24s | unavailable / 2h 38m 10s ongoing | unavailable / — |
| `04b165a3` | 2,506 | 47m 33s / 23h 21m 54s | unavailable / 1h 50m 32s ongoing | unavailable / — |

**rajatverma57 — totals**

| Created (Stockholm) | Wave | Members | State | Recorded processing | Wall after intake closed | Upload start → end |
|---|---|---:|---|---:|---:|---:|
| Oct 24 21:05 | `3a2dac1a` | 0 | complete | bypassed | 0s | 1s |
| Oct 24 21:07 | `12ace6ca` | 32 | complete | unavailable | 2h 27m 46s | 2h 30m 13s |
| Oct 24 22:05 | `3696da99` | 400 | complete | unavailable | 2h 8m 42s | 2h 11m 22s |
| Oct 24 22:16 | `aacb43ab` | 1 | complete | unavailable | 13h 12m 45s | 13h 12m 49s |
| Oct 24 22:17 | `779fdea7` | 1 | complete | unavailable | 13h 12m 14s | 13h 12m 16s |
| Oct 25 15:40 | `e6f6d061` | 18 | complete | unavailable | 5h 17m 8s | 5h 27m 57s |
| Oct 25 15:52 | `10922c76` | 0 | complete | bypassed | 5h 16m 6s | 5h 16m 7s |
| Oct 25 17:24 | `e45117de` | 621 | complete | unavailable | 4h 6m 22s | 4h 53m 38s |
| Oct 26 14:56 | `83920728` | 1,009 | complete | unavailable | 46m 33s | 54m 11s |
| Oct 26 17:54 | `51ad8be7` | 1,072 | complete | ≥ 17m 9s | 6h 15m 47s | 6h 24m 12s |
| Oct 26 21:46 | `db1132b4` | 1,501 | complete | ≥ 5m 22s | 11h 52m 33s | 12h 3m 34s |
| Oct 27 09:09 | `82a439bc` | 1,495 | complete | ≥ 5m 25s | 55m 19s | 1h 5m 45s |
| Oct 27 12:39 | `658bc129` | 2,835 | complete | ≥ 47m 44s | 1h 14m 14s | 1h 36m 11s |
| Oct 27 15:30 | `b288f7ba` | 2,162 | complete | ≥ 12m 51s | 23m 54s | 30m 4s |
| Oct 27 16:34 | `39e91938` | 1,480 | complete | ≥ 25m 30s | 38m 33s | 47m 11s |
| Oct 27 19:16 | `42cc98bb` | 6,739 | complete | ≥ 58m 46s | 1h 35m 23s | 3h 38m 43s |
| Oct 28 13:31 | `1a26f5bf` | 797 | complete | ≥ 16m 2s | 24m 9s | 2h 22m 6s |
| Oct 28 18:18 | `ad7c77ac` | 2,358 | complete | ≥ 31m 37s | 1h 0m 32s | 2h 17m 3s |
| Oct 28 21:19 | `d4bf936b` | 1 | complete | bypassed | 0s | 2s |
| Oct 28 21:19 | `dcce8178` | 195 | complete | ≥ 3m 12s | 5m 52s | 9m 9s |
| Oct 28 21:32 | `5fd9778e` | 208 | complete | ≥ 3m 5s | 5m 20s | 10m 47s |
| Oct 28 23:04 | `a2445151` | 984 | complete | ≥ 1h 16m 42s | 9h 52m 54s | 10h 13m 59s |
| Oct 29 00:43 | `8e80256e` | 0 | complete | ≥ 0s | 8h 35m 46s | 8h 35m 49s |
| Oct 29 00:43 | `f5425951` | 9,731 | complete | ≥ 2h 23m 38s | 11h 17m 5s | 12h 3m 54s |
| Oct 29 17:17 | `3ce20e24` | 228 | complete | ≥ 3m 16s | 7m 27s | 24m 5s |
| Oct 29 18:02 | `dc2d2f13` | 666 | complete | ≥ 7m 39s | 15m 28s | 26m 42s |
| Oct 29 18:30 | `8ec251f7` | 1,000 | complete | ≥ 6m 43s | 24m 47s | 27m 30s |
| Oct 29 18:33 | `3d7350cc` | 1,001 | complete | ≥ 10m 48s | 3h 40m 56s | 3h 47m 49s |
| Oct 29 22:02 | `5e194599` | 1,001 | complete | ≥ 11m 53s | 29m 38s | 34m 0s |
| Oct 29 22:08 | `84efe784` | 22 | complete | ≥ 52s | 23m 37s | 29m 26s |
| Oct 30 14:39 | `499b7848` | 1,670 | complete | ≥ 17m 35s | 33m 49s | 45m 33s |
| Oct 30 16:17 | `a4e4ef24` | 1,432 | complete | ≥ 12m 20s | 33m 27s | 42m 44s |
| Oct 31 12:34 | `be6ccfc9` | 3,907 | complete | ≥ 34m 9s | 1h 5m 21s | 1h 27m 2s |
| Oct 31 17:55 | `7e24d04d` | 640 | complete | ≥ 5m 4s | 11m 16s | 29m 8s |
| Oct 31 21:11 | `f7d74a2e` | 0 | complete | bypassed | 0s | 13m 35s |
| Oct 31 21:16 | `4c331e32` | 0 | complete | bypassed | 0s | 33m 40s |
| Oct 31 22:42 | `1c5c23d0` | 0 | complete | bypassed | 0s | 14m 31s |
| Oct 31 22:47 | `2bd57197` | 0 | complete | bypassed | 15s | 9m 30s |
| Oct 31 23:11 | `39c7cb77` | 178 | complete | 7m 33s | 1h 27m 5s | 1h 59m 57s |
| Oct 31 23:36 | `9657cc98` | 19 | complete | 3m 36s | 1h 40m 56s | 1h 41m 19s |
| Oct 31 23:38 | `923a1457` | 2,688 | complete | 49m 15s | 2h 59m 16s | 3h 50m 30s |
| Nov 01 00:19 | `5747e1fd` | 1,466 | cancelled | ≥ 15m 37s | 10h 3m 9s | 10h 48m 32s |
| Nov 01 01:28 | `557bd602` | 1 | cancelled | unavailable | 9h 39m 27s | 9h 39m 28s |
| Nov 01 01:28 | `39a1ed08` | 155 | cancelled | unavailable | 8h 13m 6s | 9h 39m 10s |
| Nov 03 18:06 | `d70d6045` | 545 | complete | 20m 40s | 22m 0s | 31m 54s |
| Nov 03 18:19 | `2046c2e4` | 15 | complete | ≥ 40s | 18m 28s | 18m 59s |
| Nov 03 18:28 | `b906ae25` | 1,687 | complete | ≥ 1h 0m 2s | 1h 11m 29s | 1h 29m 21s |
| Nov 03 18:49 | `6a122078` | 3,100 | complete | 1h 24m 14s | 1h 58m 54s | 2h 18m 29s |
| Nov 03 22:53 | `8362a288` | 26 | complete | ≥ 1m 44s | 3m 56s | 6m 45s |

**rajatverma57 — stage detail**

Each stage cell is **recorded processing / wall time**.

| Wave | Members | Vision | Clustering | Indexing |
|---|---:|---|---|---|
| `3a2dac1a` | 0 | bypassed | bypassed | bypassed |
| `12ace6ca` | 32 | unavailable / 2h 1m 7s | unavailable / 8s | unavailable / 26m 31s |
| `3696da99` | 400 | unavailable / 2h 2m 33s | unavailable / 3m 20s | unavailable / 2m 49s |
| `aacb43ab` | 1 | unavailable / 2h 1m 3s | unavailable / 0s | unavailable / 11h 11m 41s |
| `779fdea7` | 1 | unavailable / 13h 12m 0s | unavailable / 0s | unavailable / 14s |
| `e6f6d061` | 18 | unavailable / 5h 16m 24s | unavailable / 1s | unavailable / 44s |
| `10922c76` | 0 | bypassed | bypassed | bypassed |
| `e45117de` | 621 | unavailable / 3h 58m 53s | unavailable / 2m 40s | unavailable / 4m 49s |
| `83920728` | 1,009 | unavailable / 24m 26s | unavailable / 17m 33s | unavailable / 4m 34s |
| `51ad8be7` | 1,072 | unavailable / 2h 17m 43s | 17m 9s / 18m 2s | unavailable / 3h 40m 2s |
| `db1132b4` | 1,501 | unavailable / 1h 37m 27s | 5m 22s / 10h 7m 56s | unavailable / 7m 10s |
| `82a439bc` | 1,495 | unavailable / 39m 7s | 5m 25s / 5m 53s | unavailable / 10m 19s |
| `658bc129` | 2,835 | ≥ 40m 22s / 48m 18s | 7m 22s / 8m 16s | unavailable / 17m 41s |
| `b288f7ba` | 2,162 | 6m 48s / 7m 49s | 6m 3s / 6m 43s | unavailable / 9m 21s |
| `39e91938` | 1,480 | 20m 46s / 22m 19s | 4m 44s / 5m 39s | unavailable / 10m 35s |
| `42cc98bb` | 6,739 | 42m 26s / 44m 32s | 16m 21s / 17m 8s | unavailable / 33m 43s |
| `1a26f5bf` | 797 | 14m 35s / 16m 25s | 1m 27s / 4m 3s | unavailable / 3m 41s |
| `ad7c77ac` | 2,358 | ≥ 27m 47s / 42m 11s | 3m 50s / 4m 34s | unavailable / 13m 48s |
| `d4bf936b` | 1 | bypassed | bypassed | bypassed |
| `dcce8178` | 195 | 2m 32s / 3m 16s | 40s / 1m 25s | unavailable / 1m 10s |
| `5fd9778e` | 208 | 2m 27s / 3m 17s | 38s / 46s | unavailable / 1m 17s |
| `a2445151` | 984 | 1h 15m 26s / 9h 45m 48s | 1m 17s / 2m 14s | unavailable / 4m 52s |
| `8e80256e` | 0 | 0s / 8h 27m 58s | unavailable / 7m 5s | unavailable / 42s |
| `f5425951` | 9,731 | ≥ 2h 0m 28s / 9h 52m 26s | 23m 10s / 23m 40s | unavailable / 1h 0m 59s |
| `3ce20e24` | 228 | 2m 36s / 4m 34s | 40s / 48s | unavailable / 2m 5s |
| `dc2d2f13` | 666 | 5m 58s / 9m 12s | 1m 41s / 2m 27s | unavailable / 3m 50s |
| `8ec251f7` | 1,000 | 4m 21s / 9m 58s | 2m 22s / 2m 29s | unavailable / 12m 21s |
| `3d7350cc` | 1,001 | 8m 18s / 3h 33m 28s | 2m 30s / 2m 51s | unavailable / 4m 38s |
| `5e194599` | 1,001 | 9m 41s / 21m 34s | 2m 12s / 2m 48s | unavailable / 5m 16s |
| `84efe784` | 22 | 13s / 14m 3s | 39s / 8m 57s | unavailable / 37s |
| `499b7848` | 1,670 | 13m 42s / 20m 41s | 3m 53s / 4m 42s | unavailable / 8m 26s |
| `a4e4ef24` | 1,432 | ≥ 8m 36s / 21m 4s | 3m 45s / 4m 19s | unavailable / 8m 5s |
| `be6ccfc9` | 3,907 | ≥ 25m 19s / 35m 15s | 8m 50s / 11m 17s | unavailable / 18m 50s |
| `7e24d04d` | 640 | 1m 53s / 3m 31s | 3m 11s / 3m 42s | unavailable / 4m 4s |
| `f7d74a2e` | 0 | bypassed | bypassed | bypassed |
| `4c331e32` | 0 | bypassed | bypassed | bypassed |
| `1c5c23d0` | 0 | bypassed | bypassed | bypassed |
| `2bd57197` | 0 | bypassed | bypassed | bypassed |
| `39c7cb77` | 178 | 3m 15s / 1h 21m 32s | 2m 19s / 2m 55s | 2m 0s / 2m 39s |
| `9657cc98` | 19 | 11s / 1h 29m 12s | 2m 3s / 9m 29s | 1m 22s / 2m 15s |
| `923a1457` | 2,688 | 35m 32s / 2h 42m 35s | 6m 20s / 6m 57s | 7m 23s / 9m 44s |
| `5747e1fd` | 1,466 | ≥ 15m 37s / — | unavailable / — | unavailable / — |
| `557bd602` | 1 | unavailable / — | unavailable / — | unavailable / — |
| `39a1ed08` | 155 | unavailable / — | unavailable / — | unavailable / — |
| `d70d6045` | 545 | 18m 52s / 18m 54s | 33s / 1m 12s | 1m 15s / 1m 54s |
| `2046c2e4` | 15 | 28s / 15m 38s | unavailable / 2m 36s | 12s / 13s |
| `b906ae25` | 1,687 | ≥ 38m 58s / 44m 35s | 12m 44s / 18m 8s | 8m 19s / 8m 47s |
| `6a122078` | 3,100 | 44m 51s / 1h 6m 56s | 26m 5s / 37m 58s | 13m 18s / 14m 0s |
| `8362a288` | 26 | 51s / 1m 31s | unavailable / 40s | 54s / 1m 44s |

**Evidence and method**

Wave milestones and execution ownership were read exhaustively from Azure Table Storage for both libraries. Retained Log Analytics stage starts and summary events were queried from October 23, 2026 onward. Each attempt summary is counted once, identified by wave and stage; nested phase metrics and per-photo metrics are excluded from stage totals. A stage summary can include failed or replayed execution, so active time is effort spent, not exclusively useful new work.

Private evidence: `.azure/upload-wave-review-20261103/two-library-state-1730.json`, `all-waves-processing-history.json` and its matching `.kql`, and `all-wave-timing-metrics.json`. No cloud state or queues were changed.
