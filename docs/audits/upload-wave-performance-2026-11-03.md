# Recent upload-wave performance — November 3, 2026

Read-only live Azure Log Analytics and paginated `photojobs` table inspection at approximately 00:45–00:49 UTC (01:45–01:49 Stockholm). No deployment, queue mutation, or data repair. Raw query bodies and responses: `.azure/upload-wave-review-20261103/`.

## Current outcome

The recent upload sequence has not completed. Wave `6d61cb0ac5214365995d4288b519ea16`, 10,207 members across three sessions, is still `processing_pending` in the live durable record. It was created November 2 at 13:47:13 UTC; its last durable progress timestamp is 21:33:18 UTC. No cluster/index completion for this wave was found.

Four subsequent waves are `waiting_predecessor`:

| Wave prefix | Members |
|---|---:|
| 88397a60 | 9,980 |
| 5c061254 | 10,035 |
| 01937bc4 | 8,661 |
| 04b165a3 | 2,506 |
| Total | 31,182 |

All five unfinished waves have zero open sessions. The combined unfinished membership is 41,389; this is a wave membership count, not a deduplicated-photo count.

Vision summaries for the blocking wave record 38 attempts, 3,281 source attempts, 49m47s of summed attempt runtime, 388,398 storage calls and 160,762 job reads. Source attempts are not a unique-completion count, and summed runtime excludes gaps and interrupted attempts. The last summary is 21:32:49 UTC. No source errors, storage throttling, or server errors were recorded in those summaries. At 21:52 UTC recovery reported one pending vision handoff, one failed dispatch, no active stage lease and approximately 18m42s without progress. This identifies the blocking boundary, not a proven underlying root cause.

The vision app continues emitting logs through 00:45 UTC. Latest sampled logs show tag-maintenance retries with `index_work.WorkLeased: Maintenance work is already leased`, retained queue receipts, and delivery counts above 500/800. Worker activity therefore does not establish wave progress. The relationship between this maintenance churn and the stalled wave is not proven by this inspection.

## Latest completed wave

Wave `852ae63d0d12447cbe79c618e94798f8`: 1,898 members, zero failed members, completion outcome `applied`, completed November 2 at 15:26:01 UTC (16:26 Stockholm).

| Stage | Measured attempt runtime | Storage calls | Job reads | Outcome |
|---|---:|---:|---:|---|
| Vision | 27m05s summed across 23 attempts | 224,812 | 93,856 | Ready; 1,898 source attempts, zero source errors |
| Cluster | 15m30s | 74,659 | 39,482 | 1,902 faces processed; handed to indexing |
| Index | 12m59s | 54,906 | 33,197 | Complete |

Vision's measured throughput was approximately 70 source attempts/minute. Total measured stage attempt time was 55m34s, while creation-to-completion elapsed time was 5h26m27s. These differ because measured attempt time excludes queue/predecessor waits, pauses and interruptions; it is not end-to-end latency. No storage throttling or server errors were recorded in these summaries.

### Timestamp recheck: why 1,898 items spanned 5h26m

The five-hour figure is wave creation-to-completion latency, not continuous processing. Exact timestamps below are Stockholm time on November 2 (UTC+1):

| Milestone | Stockholm time |
|---|---|
| Wave created | 10:59:34 |
| Predecessor vision satisfied | 12:15:07 |
| First measured vision attempt began | 12:15:10 |
| Vision error/retry burst ended | 12:26:12 |
| Next measured vision attempt began | 15:39:52 |
| Vision ready | 15:57:01 |
| Cluster began | 15:57:31 |
| Cluster finished | 16:13:01 |
| Index finished / wave complete | 16:26:01 |

There was approximately 1h15m before this wave began vision, including predecessor gating. More significantly, vision summaries show a 3h13m40s gap between the 11:26:12 UTC error attempt and the next attempt starting at 14:39:52 UTC. There are no measured vision attempts in that interval. The reason for that interruption is not established by these summaries. Several attempt outcomes are errors despite zero per-source errors; these counters describe different failure boundaries.

The measured vision attempt runtime remains 27m05s, cluster 15m30s and index 12m59s: approximately 55m34s total. Approximately 4h31m of the 5h26m lifetime falls outside measured stage attempts. This wave is evidence of a long interruption and waiting time, not a five-hour compute requirement for 1,898 items.

Cluster phases: assignment persistence 550.6s, snapshot preparation 261.8s, face matching 10.1s. Assignment persistence accounted for approximately 59% of stage runtime and 64,295 attributed storage calls. Phase instrumentation is inclusive; nested phase totals must not be added blindly.

Index phases: preparation 363.1s (6m03s), 35,222 attributed storage calls; individual publication scopes together approximately 142.3s (2m22s). These phases do not cover the entire stage. Overall index process CPU was 206.5s versus 779.2s elapsed, consistent with substantial non-CPU overhead but not a precise exclusive I/O allocation.

## Earlier completed waves in the same observation window

| Members | Cluster | Index | Durable failed members |
|---|---:|---:|---:|
| 5,181 (`b558b517`) | 35m49s | 25m53s | 8; applied with failures |
| 3,489 (`36c133ee`) | 1m48s | 16m50s | 0 |
| 1,898 (`852ae63d`) | 15m30s | 12m59s | 0 |

Clustering varies substantially between waves; the smaller wave was not automatically faster. The latest completed wave's phase measurements show persistence/preparation dominate matching. The immediate operational issue is the pending vision handoff and predecessor backlog, while storage/coordination overhead is the main measured downstream performance cost.

## Deployment correlation follow-up

Live system and console log queries establish the following November 2 timeline (Stockholm time):

- 12:25: wave telemetry reports `commit_observations.commit` exhausted 16 HTTP 412 transaction retries at operation index 0. Nearby library-lease errors were initially associated with this wave incorrectly; the wave-specific telemetry establishes the page-commit conflict.
- 12:26: five vision attempts have failed; logs then report `Wave dispatch attempts exhausted; operator retry required`.
- 12:31: revision 0000054 undergoes a replica stop/restart; system logs include node scale down. This is after dispatch exhaustion, with the same image/revision, and is not evidence of a new deployment causing the initial error.
- 14:44: revision 0000055 is created, replacing vision image 7e4b046 with e287fc3. Its main image starts at 14:46. This rollout is approximately 2h18m after the wave stopped.
- 15:39:52: the wave resumes on revision 0000055 with dispatch attempt 1, approximately 55 minutes after revision creation.

Thus deployments occurred during the interruption, but the observed wave failure was page-evidence transaction contention followed by exhausted dispatch retries. The earlier attribution to library-assignment lease contention is superseded by the wave-specific investigation in `vision-failure-root-cause-2026-11-03.md`. The revision timing does not support attributing the entire three-hour gap to deployment startup or a rollout killing active vision work. The evidence does not establish what reset/reissued dispatch, or whether a deployment indirectly influenced lease contention. The 5h26m lifetime remains an incident-affected latency measurement; measured stage runtime is the more useful throughput comparison.

Evidence: `gap-system`, `gap-exception`, `gap-resume`, and `gap-retry` query files and results under the raw evidence directory.

## Ten-hour vision status refresh — 02:00 Stockholm, November 3

Window: November 2 16:00 through November 3 02:00 Stockholm (15:00–01:00 UTC). Global Log Analytics queries over wave summaries, starts and recovery events found the following waves. Durable records for the observed library were refreshed with pagination.

| Wave prefix | Members | Created, Stockholm Nov 2 | Current vision status |
|---|---:|---|---|
| 6d61cb0a | 10,207 | 14:47, carried into window | processing_pending; last durable progress 22:33 |
| 88397a60 | 9,980 | 19:26 | waiting_predecessor |
| 5c061254 | 10,035 | 20:16 | waiting_predecessor |
| 01937bc4 | 8,661 | 21:08 | waiting_predecessor |
| 04b165a3 | 2,506 | 22:38 | waiting_predecessor |

No wave completed vision inside the window. All four waves created inside the window are blocked, with no vision-attempt summaries found for them. The carried-over 10,207-member wave recorded 37 vision-attempt summaries, including 12 error outcomes, 3,178 source attempts and 48m06s summed attempt runtime inside the window. Source attempts are not a deduplicated completion count. Its most recent recovery snapshot (22:52 Stockholm) reports a failed dispatch and no active stage lease; the durable record remains processing_pending at refresh.

Wave 852ae63d (1,898 members) completed its full pipeline at 16:26 Stockholm within the window, but vision had already finished at 15:57, approximately three minutes before the window began. It therefore counts as completed overall in this window, not a new vision completion in this window.

Total unfinished wave membership is 41,389, comprising 10,207 in the blocking wave and 31,182 in its four successors. Member counts do not imply that every item in the partially processed blocking wave still needs inference. Evidence: `wave-state-10h.json`, `ten-hour-waves*`, and `ten-hour-vision*` in the raw evidence directory.
