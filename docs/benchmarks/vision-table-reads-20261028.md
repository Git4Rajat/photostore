# Vision Table read comparison, 2026-10-28

This was the initial narrow comparison. See the [expanded suite](vision-optimization-suite-20261028.md)
for mixed proof/inbox queries, sorted key families, real receipts, Azure runs,
and the resulting optimization recommendations.

Read-only comparison against the existing forenklaprep1 photojobs wave partition.
The first 200 members of wave 42cc98bb4e854c48a03a22593b654d59 supplied member
keys and their corresponding deletion-proof keys. Each variant read the same
20 or 200 keys, three rounds with alternating execution order. Full entity
payload fingerprints matched between query and point-read variants each round.

Measured from the developer workstation, not from an Azure worker. These are
request-path measurements, not a reproduction of the complete inspection phase.
They do not cover metadata, completion inboxes, manifests, face receipts, or
small-wave partitions. No deployment, storage mutation, or role change occurred.

| Workload | Keys | Current queries, median ms | Four point workers, ms | Eight point workers, ms | Query / point HTTP responses |
|---|---:|---:|---:|---:|---:|
| Members | 20 | 173.6 | 232.9 | 105.1 | 2 / 20 |
| Members | 200 | 931.7 | 1562.4 | 847.3 | 15 / 200 |
| Deletion proofs | 20 | 103.9 | 182.8 | 100.5 | 2 / 20 |
| Deletion proofs | 200 | 872.4 | 1499.1 | 798.3 | 15 / 200 |

No 429 or 503 response was observed. An eight-worker proof round took 1134.5 ms
for 20 keys, showing that median improvements do not eliminate tail latency.
Response-body bytes exclude headers, request bodies, and transport overhead.
At 200 keys, member queries returned 175273 bytes versus 194283 for point reads;
proof queries returned 113528 bytes versus 132538 for point reads.

The 200-key improvement with eight point workers is approximately 8–9%, at
13.3 times the HTTP response count. This evidence does not justify replacing
all key queries with point reads, nor attribute the prior 848 seconds of
inspection preparation to OR-query scans alone.

First implementation retains bounded immutable receipts across pages within
one Vision attempt, with the existing 4096-row eviction limit. Mutable
inspection snapshots are rebuilt each page; missing receipts remain uncached.
New nested timings separate member fetch, metadata fetch, proof/inbox fetch,
manifest fetch, and face receipt fetch. They are components of existing outer
timings and must not be added to those outer timings.

Next measurements should run inside the Azure network with actual metadata,
inbox, and receipt key sets. Larger contiguous member fetching, disk snapshots,
inspection overlap, and model scheduling remain subsequent work; this result
does not measure their impact or predict a full-wave speedup.
