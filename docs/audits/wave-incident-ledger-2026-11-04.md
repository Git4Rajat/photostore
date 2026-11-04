# Per-wave incident ledger — 2026-11-04

Scope: all **61 persisted waves** across the two libraries, not only the recent upload batch. Durable state snapshot is approximately 2026-11-03 23:13 UTC; telemetry investigation extends to approximately 23:18 UTC. This is an incident snapshot, not a continuing live status feed.

There are 46 complete and three cancelled rajatverma57 waves, and nine complete and three ready hack4heart waves. “Complete” includes completion with failed members. Queue eligibility and predecessor waits are distinct from the broad persisted `ready` state.

Evidence: paginated wave/session/execution reads; 202 failed-member settlements; current-generation comparison; structured Log Analytics summaries and targeted raw logs. Source filenames, hashes and URLs are omitted. Full wave IDs are retained for operational lookup. The action IDs refer to [the action plan](wave-reliability-action-plan-2026-11-04.md).

**Interpretation:** no observed errors means no matching retained error events; it does not certify a clean run. Expired upload sessions can be normal abrupt-upload recovery. Event counts are attempts, not distinct failed photos. Older missing summaries and interrupted attempts make processing times lower bounds. A later missing/replaced source does not prove historical deletion caused a failure.

**Settlement reconciliation:** 124 required-step retry exhaustion, 64 source retry exhaustion, 10 unqualified identities and four operator-release records = 202 records. Two records belong to a cancelled wave; current wave failed-member totals therefore sum to 200. Source state now: 68 same generation, 49 different generation, 85 missing. These are wave-member records, not guaranteed distinct photos.

## rajatverma57

| Created UTC | Wave ID | Members / state / failed members | Evidence and disposition |
|---|---|---|---|
| Oct 24 20:05 | `3a2dac1abf1a4d94b596ee776032529b` | 0 / complete / — | Durable no-work completion; maintain zero-work fast-path regression. V2. |
| Oct 24 20:07 | `12ace6ca3bee4780a4590f2ee5fff70f` | 32 / complete / 4 | Settlements: 4 step retry exhausted. D1. |
| Oct 24 21:05 | `3696da9954a44a328dead4dadc46f545` | 400 / complete / 5 | Settlements: 5 step retry exhausted. D1. |
| Oct 24 21:16 | `aacb43aba0b74b4da84db8677b6cda3a` | 1 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 24 21:17 | `779fdea7b8df4fe79186fbd078006251` | 1 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 25 14:40 | `e6f6d0610479484caddb380f741df945` | 18 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 25 14:52 | `10922c76d5a343c4a3fe523abd764a25` | 0 / complete / — | Durable no-work completion; maintain zero-work fast-path regression. V2. |
| Oct 25 16:24 | `e45117dec6b74bec8452bf22c6762bff` | 621 / complete / 37 | Settlements: 37 step retry exhausted. D1. |
| Oct 26 13:56 | `83920728e9f14b6daf2d049d2090a93f` | 1,009 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 26 16:54 | `51ad8be70977431d9999cffacb4c1b01` | 1,072 / complete / 20 | Settlements: 20 step retry exhausted. D1. |
| Oct 26 20:46 | `db1132b4570849de99d40015fefce2db` | 1,501 / complete / 0 | Five cluster error summaries Oct 26; historical exception cause not established by this audit. C1, O2. Retained error events: cluster_wave_summary=5. |
| Oct 27 08:09 | `82a439bce7d145ed977579f791c7ca12` | 1,495 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 27 11:39 | `658bc1299273498594a3a99a067e3388` | 2,835 / complete / 2 | Settlements: 2 source retry exhausted. D1. |
| Oct 27 14:30 | `b288f7bad916408d8d80bd237372d943` | 2,162 / complete / 18 | Settlements: 18 step retry exhausted. D1. |
| Oct 27 15:34 | `39e91938bc3f4f7f85346f54246658e1` | 1,480 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 27 18:16 | `42cc98bb4e854c48a03a22593b654d59` | 6,739 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 28 12:31 | `1a26f5bf64dc46368f6d87741569005c` | 797 / complete / 2 | Settlements: 2 step retry exhausted. D1. |
| Oct 28 17:18 | `ad7c77acbfb548f49b1aa1db5e885419` | 2,358 / complete / 5 | Settlements: 3 step retry exhausted, 2 source retry exhausted. D1. Retained error events: vision_wave_attempt_summary=1. |
| Oct 28 20:19 | `d4bf936bdfea4d69ad937484a7d49a50` | 1 / complete / — | Durable no-work completion; maintain zero-work fast-path regression. V2. |
| Oct 28 20:19 | `dcce817808dc4b1bbae3a969f9675eec` | 195 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 28 20:32 | `5fd9778ee793445c991c1f8dfe63c474` | 208 / complete / 11 | Settlements: 11 step retry exhausted. D1. |
| Oct 28 22:04 | `a24451515ecd4fdb98096f981b4c8de5` | 984 / complete / 4 | Four operator-release settlements for replaced sources; administrative disposition, not four diagnosed model failures. D1. Settlements: 4 operator release/replaced source. D1. Retained error events: vision_coordination_retry=4, vision_wave_attempt_summary=5. Session-expiry events: 2; expiry alone is not a failure. |
| Oct 28 23:43 | `8e80256e8b5847b7b2f7aef76baf1f10` | 0 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 28 23:43 | `f54259517fe84c19864ec308b0682ad8` | 9,731 / complete / 3 | Two coordination retries and three Vision error summaries; source errors also observed. Three terminal source retries. V1, D1. Settlements: 3 source retry exhausted. D1. Retained error events: vision_coordination_retry=2, vision_wave_attempt_summary=3. Session-expiry events: 1; expiry alone is not a failure. |
| Oct 29 16:17 | `3ce20e24d4a5481893aeef3daa7b3dcf` | 228 / complete / 3 | Settlements: 3 step retry exhausted. D1. Session-expiry events: 1; expiry alone is not a failure. |
| Oct 29 17:02 | `dc2d2f130d174596a66d128810b6df4e` | 666 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 29 17:30 | `8ec251f7519f45ea9888fe886aa588c8` | 1,000 / complete / 0 | Retained error events: vision_wave_attempt_summary=1. |
| Oct 29 17:33 | `3d7350cc938347cfaea67b4e9254fcc4` | 1,001 / complete / 6 | Settlements: 6 source retry exhausted. D1. Retained error events: vision_coordination_retry=4, vision_wave_attempt_summary=7. |
| Oct 29 21:02 | `5e19459990b84d3bbbfff13322defdf2` | 1,001 / complete / 0 | Retained error events: vision_wave_attempt_summary=2. |
| Oct 29 21:08 | `84efe7848d1e40ffab50a4b829f698e3` | 22 / complete / 0 | Session-expiry events: 1; expiry alone is not a failure. |
| Oct 30 13:39 | `499b784871ce4c81b43a92adbde13e93` | 1,670 / complete / 0 | Retained error events: vision_wave_attempt_summary=1. |
| Oct 30 15:17 | `a4e4ef249cbd46ba948b889f2831b742` | 1,432 / complete / 2 | Settlements: 2 source retry exhausted. D1. Retained error events: vision_wave_attempt_summary=1. |
| Oct 31 11:34 | `be6ccfc9ac064e16bff1b38b07a7830a` | 3,907 / complete / 0 | One short Vision error summary with zero source attempts; insufficient retained evidence for root cause. D1, O2. Retained error events: vision_wave_attempt_summary=1. |
| Oct 31 16:55 | `7e24d04d6a9e4f2b9a84bfa70774bb6f` | 640 / complete / 0 | Session-expiry events: 2; expiry alone is not a failure. |
| Oct 31 20:11 | `f7d74a2e73e74c21ac9709fe35f2798d` | 0 / complete / — | Session-expiry events: 1; expiry alone is not a failure. |
| Oct 31 20:16 | `4c331e32ee9c4fcabf0f89c0756fb940` | 0 / complete / — | Session-expiry events: 1; expiry alone is not a failure. |
| Oct 31 21:42 | `1c5c23d004454befa6309172309833ba` | 0 / complete / — | Session-expiry events: 1; expiry alone is not a failure. |
| Oct 31 21:47 | `2bd57197c3d14c7cb599f0541631203d` | 0 / complete / — | Session-expiry events: 2; expiry alone is not a failure. |
| Oct 31 22:11 | `39c7cb7737e1441bb1c3973fa1326740` | 178 / complete / 0 | Retained error events: vision_coordination_retry=5, vision_wave_attempt_summary=5. Session-expiry events: 4; expiry alone is not a failure. |
| Oct 31 22:36 | `9657cc9888864c7d98a0127010f8afb7` | 19 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Oct 31 22:38 | `923a145713fd417e908a8abb08e61624` | 2,688 / complete / 2 | Settlements: 2 identity unqualified. D1. Retained error events: vision_coordination_retry=5, vision_wave_attempt_summary=6. Session-expiry events: 1; expiry alone is not a failure. |
| Oct 31 23:19 | `5747e1fd1aef41ddba2b5c19e31a1f95` | 1,466 / cancelled / — | Settlements: 2 source retry exhausted. D1. Retained error events: vision_coordination_retry=4, vision_wave_attempt_summary=4. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 01 00:28 | `557bd60209704f66a23215139078c76a` | 1 / cancelled / — | Cancelled; do not label successful processing or automatically replay. D1. |
| Nov 01 00:28 | `39a1ed089d58468b9f56f2b5cac3265f` | 155 / cancelled / — | Session-expiry events: 1; expiry alone is not a failure. |
| Nov 03 17:06 | `d70d604554784ba6a2e75bbe70767d8e` | 545 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Nov 03 17:19 | `2046c2e4b7024dacb1784c5f732aca6c` | 15 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Nov 03 17:28 | `b906ae25a217430d95e165e9dacd18e8` | 1,687 / complete / 0 | Abrupt upload: expired session closed intake and cancelled unresolved reservations; wave completed with zero failed members. Expected recovery path, retain as canary. R1, O2. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 03 17:49 | `6a12207894d3476c9ce2861be3d87fb4` | 3,100 / complete / 0 | Abrupt upload: two unresolved reservations cancelled after session expiry; ordered predecessor wait; wave completed with zero failed members. Expected recovery path. R1, O2. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 03 21:53 | `8362a28899704523835547be147bde98` | 26 / complete / 4 | Four same-generation failed sources remain; current failed steps include OCR, AI, face, EXIF and map. Requires per-step diagnosis, not blind wave replay. V3, D1. Settlements: 4 step retry exhausted. D1. |

## hack4heart

| Created UTC | Wave ID | Members / state / failed members | Evidence and disposition |
|---|---|---|---|
| Oct 29 13:33 | `91ecc876867e45f695bae557b07e4bb0` | 1,001 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Nov 01 10:09 | `03497b116252481fa27cc95d48884b04` | 90 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Nov 01 10:10 | `f28524d6f9f4468d94460f577e81c14e` | 1,266 / complete / 0 | Session-expiry events: 1; expiry alone is not a failure. |
| Nov 01 11:10 | `33753020af04438291a3cd5cb80ae233` | 2,738 / complete / 1 | Settlements: 1 step retry exhausted. D1. Session-expiry events: 2; expiry alone is not a failure. |
| Nov 01 20:55 | `b558b5174e6e41b89fbdb1f728a31943` | 5,181 / complete / 8 | Three coordination-error attempts observed; eight terminal source retries. Complete, but full historical waiting-time attribution is unavailable. V1, D1, O2. Settlements: 8 source retry exhausted. D1. Retained error events: vision_coordination_retry=3, vision_wave_attempt_summary=3. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 01 22:32 | `36c133eea0624248a577d60b5dee3e57` | 3,489 / complete / 0 | No matching retained error events or failed-member settlements; detailed historical root-cause attribution unavailable. O2. |
| Nov 02 09:59 | `852ae63d0d12447cbe79c618e94798f8` | 1,898 / complete / 0 | Confirmed control-row CAS exhaustion; 3h13m40s Vision restart gap; 55m34s recorded work versus 5h26m intake-to-end. V1, R1. Retained error events: vision_coordination_retry=8, vision_wave_attempt_summary=8. |
| Nov 02 13:47 | `6d61cb0ac5214365995d4288b519ea16` | 10,207 / complete / 11 | Confirmed repeated commit CAS exhaustion and stranded failed intent; operator re-request required. Eleven terminal source retries. Duplicate-admission cohort. V1, V2, R1. Settlements: 11 source retry exhausted. D1. Retained error events: vision_coordination_retry=12, vision_wave_attempt_summary=12. Session-expiry events: 3; expiry alone is not a failure. |
| Nov 02 18:26 | `88397a60bdca46d1aa61aebccf6e4711` | 9,980 / complete / 3 | Rollout interruptions plus busy-lease deliveries exhausted raw queue delivery limit; stranded clustering recovered by exact replay. Index subsequently exhausted six dispatch attempts and blocked successors; individual index-attempt causes remain incomplete. R1, C1, I1. Settlements: 3 source retry exhausted. D1. Retained error events: index_wave_summary=1. Session-expiry events: 2; expiry alone is not a failure. |
| Nov 02 19:16 | `5c061254feaf4cd28a5ffc880e64c79b` | 10,035 / ready / 22 | Duplicate-admission cohort; coordination retries; terminal source/identity/step failures. Cluster slices replay 4.6k–5k-prefix for 128 new assignments. V1, V2, C1, D1. Settlements: 7 step retry exhausted, 8 identity unqualified, 7 source retry exhausted. D1. Retained error events: vision_coordination_retry=4, vision_wave_attempt_summary=4. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 02 20:08 | `01937bc49a10423d81fec2e1e91bd0c0` | 8,661 / ready / 25 | Duplicate-only upload sessions; reused receipts for some members, do not equate member count with new photos. Vision terminal failures; predecessor cluster/index publication wait. V2, I2, D1. Settlements: 8 step retry exhausted, 17 source retry exhausted. D1. Session-expiry events: 1; expiry alone is not a failure. |
| Nov 02 21:38 | `04b165a316444f3ca05c39266e3f5e96` | 2,506 / ready / 2 | Duplicate-only upload sessions; two terminal failures, predecessor publication wait after Vision. V2, I2, D1. Settlements: 1 step retry exhausted, 1 source retry exhausted. D1. |

## Evidence files and limits

Local raw evidence is retained under `.azure/upload-wave-review-20261103/` (ignored, contains operational identifiers):

- `two-library-state-1730.json`: paginated durable wave/session/execution snapshot; filename is historical, not the snapshot time.
- `wave-incident-telemetry-ledger.json` and `.kql`: grouped stage errors, coordination retries, step failures and session recovery.
- `failed-member-proof-audit.json`: all 202 terminal settlements, including reason and witness digest.
- `failed-source-current-details.json`: redacted generation reconciliation.
- `failed-ocr-classification.json`: missing OCR error specificity; no basis to call all OCR failures timeouts.
- `cluster-slice-replay-costs.json` and `.kql`: measured prefix-replay costs.
- `indexer-exhaustion-root-evidence.json` and `.kql`: retained index error evidence; insufficient to attribute every exhausted attempt.
- [Processing versus wall time](wave-processing-times-2026-11-04.md): per-wave and per-stage measured time with explicit missing-data flags.

Settlement evidence stores a witness digest, but not the detailed historical witness. Current metadata cannot replace that historical evidence after a source changes. Unknown causes are explicitly left unknown; D1/O2 close this diagnostic gap for future incidents.
