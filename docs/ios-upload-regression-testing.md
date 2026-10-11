# iOS browser upload regression investigation

## Historical baseline

The local Git reflog retains the pre-squash July source. July 21 commit
`127ab0d` and July 26 commit `5cf7fab` both used the native HTML file input,
full-file SHA-256 hashing, and direct Azure Blob uploads. July 26 has a
successful image publication run:
https://github.com/Git4Rajat/photostore/actions/runs/30216898744

These are source/build records, not proof of a particular 3,000-file upload.
No June source/build records were recovered in the investigation, and the
available LAW workspace returned no June/July console records (its current
retention is 30 days).

Later changes include the August 21 known-hash prefetch, August 22 internal
900-file session splitting, and subsequent resume/queue state fixes. Do not
roll back backend authorization or integrity changes to test an older frontend.

## Reproduced behavior and narrow change

Component tests drive the current file input with 300 and 3,000 distinct files
and mock only external storage/API/processing boundaries. All selected files
reach initialization and finalization; the 3,000-file case drains all four
internal sessions. A paused 100-file session followed by a 300-file reselection
also resumes the original 100 and uploads the additional 200.

A separate test failed before the change: a known-hash request that never
settles prevents every transfer from starting. The optional prefetch now has a
3-second end-to-end deadline, a single HTTP attempt, and cancellation. Late
responses are ignored. Backend deduplication and original SHA-256 reporting
remain unchanged. This does not establish the cause of native picker failures
or the reported 100-file selection issue.

## Real-device test

Keep the same iPhone/iPad, iOS version, browser version, library, and selected
assets for comparisons. Test 100, 300, and 3,000 items, separately for locally
available originals and iCloud-only originals. Also test a broader reselection
against a paused upload. Do not substitute a compatibility/re-encoding path.

Capture the browser return count before deduplication and resume matching:

- `upload.picker.change`: `rows` is the exact returned file count; `bytes` is
  the selected source-byte total; `ms` is time since opening the picker.
- `upload.picker.cancel`: the picker returned no new selection. This does not
  distinguish a deliberate cancel from a native acquisition failure.
- `upload.known_hashes.timeout`: the optional lookup hit its startup budget.

These use the existing client performance collector and `/api/perf/client`
allowlisted span fields. No filenames, file contents, hashes, or SAS URLs are
included in these spans. Telemetry is best-effort and can be disabled with the
existing performance switch. A browser that never emits either picker event
is still not directly observable from these spans.

After the changed frontend is deployed, inspect LAW:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h)
| where Log_s has "PERF event=client_span" and Log_s contains "name=upload."
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated asc
```

If the picker returns 100 from a 300-item selection, investigate native/provider
acquisition. If it returns 300, compare initialized/finalized/failed/duplicate
counts across every session. Synthetic component tests cannot validate the
real iOS Photos/Files picker, iCloud fetching, or original representation chosen
by the OS before files reach JavaScript.
