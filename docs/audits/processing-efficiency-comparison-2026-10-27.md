# Vision and clustering comparison — 2026-10-27

## Scope and limits

Baseline: wave `51ad8be70977431d9999cffacb4c1b01`, as documented in
`processing-efficiency-2026-10-26.md`. New run:
`db1132b4570849de99d40015fefce2db`, image `b0206ef`.
Different inputs and starting library snapshots: this is an observed production
comparison, not a controlled benchmark or proof of output parity.

Vision completed all 1,501 sources with zero failed members. Its durable Vision
lease succeeded and the wave froze at 2026-10-26 22:34:39 UTC. The old productive
Vision sample had 991 processed images; exclude its later inspection-error loop.
New Vision scope totals include all 23 queue scopes through successful freeze.

The new clustering initial attempt applied all 1,479 faces, but failed its final
publication handoff because `commit_assignment` still requires a Table manifest
while the new journal stores that manifest locally. The old stage completed with
1,041 faces. Therefore clustering numbers measure productive work through applied
assignments, not successful end-to-end wave completion. Retries are separate.

## Clustering

| Metric | Before | After, initial attempt | Reduction |
| --- | ---: | ---: | ---: |
| Faces | 1,041 | 1,479 | 42% more input |
| Elapsed seconds | 1,029.139 | 284.823 | 72.3% |
| Process CPU seconds | 280.399 | 61.533 | 78.1% |
| SDK calls | 129,852 | 19,806 | 84.7% |
| SDK calls/face | 124.74 | 13.39 | 89.3% |
| Elapsed seconds/face | 0.989 | 0.193 | 80.5% |
| Snapshot preparation seconds | 514.664 | 107.572 | 79.1% |
| Journal write seconds | 466.258 | 49.199 | 89.4% |
| Journal-attributed SDK calls | 58,116 | 207 | 99.6% |
| Runtime build seconds | 33.160 | 0.456 | 98.6% |
| Matching seconds | 4.223 | 6.159 | Input grew; per-face cost nearly unchanged |
| Assignment persistence seconds | 160.665 | 104.418 | 35.0% |

Journal SDK attribution includes checks running in that scope, not exclusively
journal-marker writes. Phase durations are inclusive and overlap: do not add
these phase totals or subtract aggregate SDK time from wall time. New matching
had zero SDK calls. Assignment persistence still made 9,429 SDK calls.

After the productive attempt failed at 23:24:24 UTC, replay attempts at 23:28:39
and 23:32:42 took 11.95 and 12.26 seconds and 170 SDK calls each, then failed the
same handoff. Other deliveries collided with the retained execution lease.
Those are integration/retry overhead, not additional productive face matching.

## Vision

New elapsed from first work to freeze was about 39m51s, approximately 37.7 images
per minute. Productive queue-scope elapsed totals were 1,265.710 seconds for the
old 991-image sample and 2,257.789 seconds for the new 1,501-image run.

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| Active scoped seconds/image | 1.277 | 1.504 | 17.8% higher |
| SDK calls/image | 137.17 | 132.26 | 3.6% fewer |
| Aggregate SDK seconds/image | 1.168 | 1.527 | 30.8% higher |
| Job-table GETs/image | 76.11 | 75.30 | 1.1% fewer |
| Face inference ms/image | 403.7 | 406.6 | Nearly unchanged |
| CLIP inference ms/image | 188.4 | 189.7 | Nearly unchanged |
| OCR ms/image | 113.3 | 111.1 | Nearly unchanged |
| Face persistence ms/image | 247.3 | 309.5 | 25.1% higher |
| Semantic refresh ms/image | 105.0 | 97.0 | 7.7% lower |

New totals: 198,522 SDK calls, 2,292.113 aggregate SDK seconds, 113,018 job-table
GETs and 1,201.102 aggregate seconds for those GETs. Mean job GET latency rose
from about 7.7 to 10.6 ms. This is consistent with storage latency contributing
to the regression; different inputs and conditions prevent causal attribution.

Newly exposed phases average 372.7 ms for derivative promotion and 248.8 ms for
artifact capture. They were not instrumented in the old sample, so no reduction
can be claimed for these phases. Their shared-scope per-image SDK deltas overlap
and must not be summed as unique requests. Total worker CPU was about 1,632
seconds, distinct from active scoped elapsed and full wall time.

## Conclusions

Clustering coordination costs dropped sharply: 3.61x shorter initial productive
pass despite more faces, or 5.13x shorter per face. Completion is still blocked
by the local-journal/Table-manifest handoff integration bug; fix and retest that
before claiming end-to-end success.

Vision has no demonstrated throughput improvement. Model costs are stable;
policy/source-fence reads remain around 75 job GETs per image. Consolidate those
reads and batch face persistence, then investigate derivative promotion/capture.
Clustering next targets are snapshot point reads and bounded assignment writes.
