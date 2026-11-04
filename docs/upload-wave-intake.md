# Growing upload intake and interruption boundaries

## Browser registration and transfers

The live upload path uses one selection operation/session identity across its
transport children. Opening is coalesced per parent. Registration starts at
selection time, concurrently with hashing/init/direct byte transfers: the first
HTTP `/items` request contains at most **20 items**, subsequent requests at most
**200**. The backend still commits bounded **20-item storage transactions**.
Each file waits only for acknowledgment of the page containing its own stable
`waveItemKey`, not completion of the entire selection.

Registration keeps immutable, in-memory item-ID/filename snapshots and keyed
acknowledgment progress. It never uses a count as an offset into a checkpoint
that can lose completed files. Retry replays unacknowledged pages idempotently;
an unsuccessful background attempt releases its admission waiters instead of
leaving them hung. Snapshots contain descriptors, not file bytes, but their
memory use still grows with selection size. They are not a new persisted schema.
After reload the remaining checkpoint can be registered idempotently by key;
the server retains previously accepted registration and finalization evidence.

Registration has a 90-second heartbeat timer even before a transfer child
starts. The active upload also renews through hashing, transfers and
finalization; concurrent renewals are coalesced. A stalled/failed `/items`
request is **not** proof that registration drained. The selection's original
`declaredCount` remains authoritative: complete registration means all those
declarations are acknowledged, not a smaller mutable checkpoint length.
Successful heartbeat renewal alone does not close intake or release Vision.

## Normal release versus interruption

The existing backend barrier holds Vision while any included intake session is
open. Normal closure requires all declared items registered and resolved.
Successful finalize responses carry durable resolution receipts; accepted and
duplicate members carry immutable photo/revision identities. No page boundary
releases a partial normal wave. Intake can grow until it is sealed; a sealed
wave cannot reopen and subsequent selections use a successor.

Stop/Discard immediately quiesces local live registration and admission. An
already-running HTTP page is allowed to finish or time out; **no remaining pages
are started**. After outstanding local finalization tasks and artifact cleanup,
the browser uses server-authoritative discard/abandonment, rather than assigning
`cancelled` to every missing browser completion. Server-accepted uploads whose
responses/checkpoints were lost therefore remain included. Unresolved
reservations are cancelled and unregistered declarations are excluded. The
existing finalization lease can defer abandonment until a previously admitted
source write completes or its lease expires. Late registration, admission and
stale source finalization cannot resurrect the sealed subset.

The existing backend session lease is **300 seconds**. Browser close, sleep, or
network loss eventually stops renewal. Core discovers sessions in the new
`photouploadsessions` table on authenticated user traffic and fences stale
intake, drains bounded abandonment pages, and releases only the finalized
accepted/duplicate subset. Browser close does not depend on a reliable unload
request. Expiry/renewal race through existing conditional entity writes. There
is no expiry queue, clustering expiry poller, or expiry scaling rule.

Upload writes the lifecycle directory before creating a live session, then
conditionally removes its entry after normal completion/explicit discard and
stage handoff. Heartbeats update the existing authoritative wave-session row;
they do not add lifecycle-table writes. Registration/resolution/source admission
piggyback server activity timestamps on existing conditional session writes.
Core claims stale directory entries with an ETag and an expiring owner token,
then checks/fences the authoritative session again. Interrupted cleanup stays
indexed and resumes on later visits. Bounded cursors cover older sessions and
large directories without starting at the same first page forever.

After confirmed stale closure and handoff, Core atomically writes a durable
notice and removes the lifecycle entry in the same table transaction. Notice
counts include failed/cancelled registrations and never-registered declarations.
The bell fetches notices from Core on foreground return, online events and
throttled user input, on every page including outside Tools. Only pending
cleanup gets bounded foreground follow-ups. Reading/dismissing retains a durable
read receipt for 14 days, preventing replay from creating duplicate notices.
Retry uses the matching local checkpoint and creates a successor session; if
files/checkpoints are absent on this device the user is asked to reselect them.
Completed photos remain saved. No bytes are reconstructed or silently forwarded
as successful work for unfinished files.

The user accepts cleanup on the next visit, resumption after interrupted Core
execution, premature closure during prolonged network loss, and no rollback of
already-published downstream work. Table Storage call totals depend on page
counts, retries and the one-time migration; ten is a typical budget, not a hard
maximum. Normal admission/settlement adds a directory create and a conditional
read/delete; heartbeats add zero calls to the new table.

A server-inactive heartbeat, or an unsuccessful heartbeat after the known
server-relative deadline, fences the ongoing browser upload. File retry lanes
never automatically create/rebind a successor. The active provider aborts
transfer/retry lanes, clears queued selections, and retains an explicit retry
checkpoint. Live-capable browser restoration also requires user retry approval.
The existing explicit Retry action may create a linked new lineage for the
unfinished subset; it does not reopen the old session.

## Limits and validation

- Cancellation is not instantaneous remote rollback: an in-flight registration
  request can run until its existing 120-second HTTP timeout, and server
  already-admitted finalizations receive a 30-second discard grace period.
- Network outages can prevent discard acknowledgment. The checkpoint is retained
  for retry; traffic-driven recovery needs functioning backend storage and a
  later user visit. It does not guarantee an exact wall-clock release time.
- Authenticated library members may discard the session whose unguessable ID
  they hold. Discard cancels only that session's unresolved reservations;
  accepted library members remain preserved.
- Advisory shadow/legacy uploads retain their previous resolution behavior;
  the terminal intake guarantees described here apply to the live protocol.
- Automated coverage includes blocked second-page early init/transfer,
  checkpoint shrink without skipped IDs, Stop with a lost page response,
  background failure waiter release, heartbeat expiry without automatic rebind,
  and backend partial accepted-member release/stale-finalize rejection. These
  are unit/integration tests with mocked transport/storage, not live deployment,
  browser-close, slow-network, or production Vision verification.
## Manual upload cleanup in Tools

Tools Overview and Tools Recovery offer **Upload cleanup**. Clicking it first
asks Core to reconcile stale intake, then calls the Upload service's owner-only
`POST /upload/cleanup` in bounded 25-record pages. It reads upload tracking from
the authenticated library's metadata, so it works after the lifecycle directory
row or local browser checkpoint has gone. No timer, queue or sidecar is added.

Cleanup waits for open intake, recent upload activity and running wave stages
to finish. A short conditional policy fence prevents new live source writes
while a page is processed. It removes abandoned tracked originals, thumbnails,
previews and metadata packets, clears stale tracking on finalized photos, and
preserves completed/renamed sources and recent per-photo processing leases.
It does not enumerate all unreferenced blobs across the storage account.

Exact pending blob identities are recorded as `uploadCleanupBlobs` on metadata
before deletion. New init calls allocate a fresh blob if the previous identity
is being cleaned. Blob or metadata failures retain that record for a later
click; legacy browser cancellation cannot erase it. Final metadata writes use
ETags. The UI reports counts, skipped records and failures, and does not report
partial cleanup as complete success. A click processes at most 100 pages; large
backlogs can be continued with another click.
