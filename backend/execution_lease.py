"""Renewable per-(library, wave, stage) ownership for wave execution -- see
docs/indexing-clustering-target-state.md step 3 part 1 and part 3.

This is distinct from TWO other leases already in the codebase, and must
stay distinct from both:

- index_work.WorkStore's claim: a short-lived DISPATCHER reservation that a
  queue send is in flight for one outbox. A successful send does not mean
  the stage's work completed -- that is exactly what this module tracks.
- storage_utils.claim_processing_lease: a per-PHOTO source-processing lease.
  A library-stage lease here covers one wave's whole stage, not one photo.

Ownership never silently survives a lease expiry, even for the same holder:
reclaiming an expired lease always mints a fresh owner id and advances the
epoch, so a holder whose own clock thinks its lease is still valid cannot
keep acting after another claim has already reclaimed it -- every mutating
call re-checks (owner, epoch) against the current durable row first.
"""
from __future__ import annotations

import time
import contextvars
import threading
from contextlib import contextmanager
import uuid
from dataclasses import dataclass

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode

PARTITION = 'upload-wave-execution'
STAGES = ('vision', 'cluster', 'index')
_ACTIVE = contextvars.ContextVar('wave_execution', default=None)


def etag(row):
    return getattr(row, 'metadata', {}).get('etag')


class OwnershipLost(RuntimeError):
    """Raised whenever a caller's (owner, epoch) is no longer authoritative,
    or the lease it holds has expired -- never silently ignored."""


@dataclass(frozen=True)
class Ownership:
    key: str
    library: str
    waveId: str
    stage: str
    inputRevision: str
    executionId: str
    owner: str
    epoch: int
    status: str


class ExecutionLeases:
    def __init__(self, table, *, clock=time.time, lease_seconds=180, partition=PARTITION):
        if table is None:
            raise ValueError('Execution leases require a table')
        self.table, self.clock, self.lease_seconds = table, clock, lease_seconds
        self.partition = partition

    @staticmethod
    def key(library, wave_id, stage):
        if stage not in STAGES:
            raise ValueError('Invalid execution stage')
        return f'{library}!{wave_id}!{stage}'

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def _replace(self, current, row):
        if current is None:
            if hasattr(self.table, 'create_entity'):
                self.table.create_entity(row)
            else:
                self.table.submit_transaction([('create', row)])
        else:
            operation = self.guard_operation(current, row)
            if hasattr(self.table, 'update_entity'):
                self.table.update_entity(row, **operation[2])
            else:
                self.table.submit_transaction([operation])

    @staticmethod
    def guard_operation(current, row=None):
        version = etag(current)
        if not version:
            raise RuntimeError('Execution record has no ETag')
        return ('update', dict(current) if row is None else row,
                dict(mode=UpdateMode.REPLACE, etag=version,
                     match_condition=MatchConditions.IfNotModified))

    def check(self, ownership):
        current = self._authoritative(ownership)
        if current.get('status') != 'running' or float(current.get('leaseUntil') or 0) <= self.clock():
            raise OwnershipLost('Execution lease already expired or finished')
        return current

    @staticmethod
    def _to_ownership(row):
        return Ownership(key=row['RowKey'], library=row['libraryId'], waveId=row['waveId'],
                         stage=row['stage'], inputRevision=row['inputRevision'],
                         executionId=row['executionId'], owner=row['owner'], epoch=row['epoch'],
                         status=row['status'])

    def claim(self, library, wave_id, stage, input_revision):
        """Conditionally claims the exact eligible execution: creates the
        durable record on a library/wave/stage's first claim, or reclaims it
        once its lease has expired -- including a same-owner reclaim, which
        still mints a brand new owner id and an advanced epoch rather than
        resuming the expired attempt. Raises OwnershipLost (not silently
        waiting) when the record is currently leased by a live holder.

        A duplicate/obsolete message for a revision that already finished
        (`status == 'succeeded'`) is a no-op: it returns the existing
        (already-released) ownership with its status intact rather than
        reclaiming and re-running finished work -- callers must check
        `.status` before redoing side effects. A genuinely NEW input
        revision (e.g. intake reopened and bumped the epoch this wave/stage
        is scoped to) always starts a fresh execution id and epoch 1,
        superseding whatever the previous revision left behind, as long as
        that previous revision is not currently live-leased.
        """
        key = self.key(library, wave_id, stage)
        for _ in range(12):
            current = self._get(key)
            now = self.clock()
            if current is not None and float(current.get('leaseUntil') or 0) > now:
                raise OwnershipLost('Stage execution is already claimed by a live owner')
            same_revision = current is not None and current['inputRevision'] == input_revision
            if same_revision and current.get('status') == 'succeeded':
                return self._to_ownership(current)
            owner = str(uuid.uuid4())
            epoch = int(current['epoch']) + 1 if same_revision else 1
            execution_id = current['executionId'] if same_revision else str(uuid.uuid4())
            row = dict(PartitionKey=self.partition, RowKey=key, libraryId=library, waveId=wave_id, stage=stage,
                       inputRevision=input_revision, executionId=execution_id, status='running',
                       owner=owner, epoch=epoch, leaseUntil=now + self.lease_seconds,
                       progressCheckpoint=(current.get('progressCheckpoint') or '') if same_revision else '',
                       lastProgressAt=now, claimedAt=now,
                       completionReceipt=(current.get('completionReceipt') or '') if same_revision else '',
                       failureReason='')
            try:
                self._replace(current, row)
                return self._to_ownership(row)
            except (ResourceExistsError, ResourceModifiedError):
                continue
        raise RuntimeError('Execution claim contention')

    def _authoritative(self, ownership):
        current = self._get(ownership.key)
        if (current is None or current['owner'] != ownership.owner or current['epoch'] != ownership.epoch
                or current['inputRevision'] != ownership.inputRevision):
            raise OwnershipLost('Execution ownership lost to a replacement claim')
        return current

    def row(self, ownership):
        """Read-only snapshot for a caller that needs the durable progress
        checkpoint before deciding where to resume -- does not itself
        prove the ownership is still live (use renew() for that)."""
        return dict(self._authoritative(ownership))

    def renew(self, ownership):
        """Must be called periodically during long work. Raises
        OwnershipLost if the lease already expired -- a renewal cannot
        resurrect a lease a replacement may already have reclaimed."""
        for _ in range(12):
            current = self._authoritative(ownership)
            now = self.clock()
            if float(current.get('leaseUntil') or 0) <= now:
                raise OwnershipLost('Execution lease already expired')
            try:
                self._replace(current, dict(current, leaseUntil=now + self.lease_seconds))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Execution renewal contention')

    @contextmanager
    def keepalive(self, ownership):
        """Renew through native FAISS training and other long blocking calls.

        A stalled SDK call cannot bless expired ownership: check() still reads
        the durable lease and rejects expiry before the next effect.
        """
        stop, errors = threading.Event(), []
        def heartbeat():
            while not stop.wait(min(30, self.lease_seconds / 3)):
                try:
                    self.renew(ownership)
                except Exception as error:
                    errors.append(error)
                    stop.set()
        def check():
            if errors:
                raise OwnershipLost('Execution heartbeat failed') from errors[0]
            self.check(ownership)
        check()
        thread = threading.Thread(target=heartbeat, name='wave-stage-lease', daemon=True)
        thread.start()
        try:
            yield check
            check()
        finally:
            stop.set()
            thread.join(timeout=1)

    def checkpoint(self, ownership, progress):
        """Persists progress so recovery does not redo completed work.
        Requires live ownership, same as renew()."""
        for _ in range(12):
            current = self._authoritative(ownership)
            if float(current.get('leaseUntil') or 0) <= self.clock():
                raise OwnershipLost('Execution lease already expired')
            try:
                self._replace(current, dict(current, progressCheckpoint=progress, lastProgressAt=self.clock()))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Execution checkpoint contention')

    def release(self, ownership):
        """Releases only THIS exact attempt. A stale ownership (already
        superseded by a reclaim) raises instead of clearing the new
        holder's claim -- an old worker's `finally` block must never
        release its replacement's claim."""
        for _ in range(12):
            current = self._authoritative(ownership)
            try:
                self._replace(current, dict(current, owner='', leaseUntil=0))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Execution release contention')

    def complete(self, ownership, receipt):
        for _ in range(12):
            current = self.check(ownership)
            try:
                self._replace(current, dict(current, status='succeeded', owner='', leaseUntil=0,
                                            completionReceipt=str(receipt), completedAt=self.clock()))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Execution completion contention')

    def fail(self, ownership, reason):
        for _ in range(12):
            current = self.check(ownership)
            try:
                self._replace(current, dict(current, status='failed', owner='', leaseUntil=0,
                                            failureReason=str(reason)[:1024]))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Execution failure contention')


@contextmanager
def execution_scope(leases, ownership, store, eligible):
    # Wave transitions and their ownership check must share one transaction.
    if leases.table is not store.table or leases.partition != store.partition:
        raise ValueError('Stage execution must share the wave table and partition')
    token = _ACTIVE.set((leases, ownership, eligible))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def guard_operations(store):
    active = _ACTIVE.get()
    if active is None:
        return []
    leases, ownership, eligible = active
    if store.table is not leases.table or store.partition != leases.partition:
        raise ValueError('Execution scope does not match wave storage')
    eligible()
    return [leases.guard_operation(leases.check(ownership))]
