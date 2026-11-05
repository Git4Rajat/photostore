# Receipt query shape on Azure, 2 vCPU / 4 GiB

An isolated job using build `704f626` compared production `read_entities`
namespace-grouped OR queries with four parallel `get_entity` calls. Actual
cgroup limits were `200000 100000` CPU and 4,294,967,296 memory bytes.
Temporary Azure Table Storage held synthetic payloads and was not production
storage. There was no managed identity or access to production queues/data.

Each case reads 20 hash-distributed `face!<sha256>` keys. Payloads contain a
synthetic 512-dimensional embedding; successful groups return 213,680 payload
bytes. Cases with four absent keys return 170,944 bytes. Partitions contain
1,000, 10,000 or 50,000 rows; setup is excluded. Three rounds alternate query
order. All returned payloads and expected missing-key counts match.

## Results

Median wall per 20-key group (fully populated requested keys):

| Partition rows | Current OR query | Four parallel point reads | Change, ratio of medians |
| --- | ---: | ---: | ---: |
| 1,000 | 20.49ms | 105.27ms | **5.14x slower** |
| 10,000 | 116.24ms | 77.39ms | **33.4% less wall** |
| 50,000 | 1,688.16ms | 67.82ms | **96.0% less wall** |

With four missing keys: 16.40→82.99ms at 1,000 rows (slower),
130.11→60.48ms at 10,000 (53.5% less), and 1,619.12→58.60ms at 50,000
(96.4% less). Missing keys do not change the correctness result.

Paired median reductions differ from ratios of the displayed medians. Fully
populated cases: -413.8%, +57.8%, +96.0%; missing-key cases: -412.0%, +60.5%,
+95.8%. The 10k case is variable and includes a 1.16s scan observation. Three
rounds establish a direction and scaling concern, not tight confidence bounds.

Point reads use 20 HTTP response-hook callbacks versus two for the query path.
They also use more client CPU: approximately 33–37ms/group at the larger sizes
versus 6–8ms for queries. Response-hook counts include observed SDK responses
and query continuation pages; they are not a claim about hidden service work.

## Interpretation

The production filter specifies a PartitionKey and multiple RowKey equalities
joined by OR. Microsoft documents that this performs a partition scan rather
than an indexed range query:
[Azure Table query design](https://learn.microsoft.com/en-us/azure/storage/tables/table-storage-design-for-query).
This experiment demonstrates the consequence as receipt partitions grow.
Namespace grouping alone does not eliminate scans within that namespace.

This is a read-component gain, not 96% faster Vision. The clean upload spent
49.94 seconds in all qualification/settlement; only a subset is affected. The
experiment does not prove that every observed production fetch delay has this
cause, nor does it prove that point reads win for all partition sizes.

Recommended implementation: avoid this query shape for large sparse receipt
lookups, preserve bounded concurrency and byte budgets, and keep efficient
queries/ranges for small or contiguous selections. Add strategy/latency/key/
payload/page counters and validate against small and large populated libraries.
Reusing just-created immutable receipts across lifecycle boundaries avoids
these reads entirely and is valuable independently of query selection.

No production code/configuration changed. Both benchmark jobs and their shared
private temporary storage account were deleted and their absence verified.
Cleanup timestamps are recorded in the raw JSON.

Raw [results](vision-receipt-query-shape-azure-20261029.json), runner
`backend/scripts/benchmark_receipt_query_shape.py`.
