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
network loss eventually stops renewal; the durable expiry queue/recovery fences
intake, drains bounded abandonment pages, and releases only the finalized
accepted/duplicate subset. Browser close does not depend on a reliable unload
request. Expiry/renewal race through existing conditional entity writes.

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
  finalization leases can delay sealing (currently up to ten minutes).
- Network outages can prevent discard acknowledgment. The checkpoint is retained
  for retry; durable expiry/recovery needs functioning backend storage/workers
  and is eventually consistent, not an exact wall-clock release guarantee.
- Existing upload services restrict abandonment to the library owner. A member
  can use the compatibility resolve/close fallback only with complete local
  registration; partial registration is never extended just to cancel it.
  Member partial-stop closure can require owner Discard or lease expiry. This
  change does not weaken authorization or introduce session-creator schema.
- Advisory shadow/legacy uploads retain their previous resolution behavior;
  the terminal intake guarantees described here apply to the live protocol.
- Automated coverage includes blocked second-page early init/transfer,
  checkpoint shrink without skipped IDs, Stop with a lost page response,
  background failure waiter release, heartbeat expiry without automatic rebind,
  and backend partial accepted-member release/stale-finalize rejection. These
  are unit/integration tests with mocked transport/storage, not live deployment,
  browser-close, slow-network, or production Vision verification.