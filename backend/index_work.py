"""Durable maintenance intents, independent of user-visible job status.

Queue messages are wakeups. A coalesced request still advances the revision,
so completing an earlier revision cannot consume edits received during a run.
The pending partition is also the recovery index for failed queue publication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableTransactionError, UpdateMode

PARTITION = 'index-maintenance-pending'


def etag(row):
    return getattr(row, 'metadata', {}).get('etag')


class DirtySnapshot(set):
    """ETag-bearing capture; a bounded read consumes at most max_rows + 1 rows."""
    def __init__(self, rows=(), *, max_rows=None, truncated=False):
        if max_rows is not None and (not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 0):
            raise ValueError('max_rows must be a nonnegative integer or None')
        self.rows = {}
        self.truncated = bool(truncated or getattr(rows, 'has_more', False) or getattr(rows, 'truncated', False))
        if isinstance(rows, DirtySnapshot):
            rows = rows.rows.values()
        for index, row in enumerate(rows):
            if max_rows is not None and index >= max_rows:
                self.truncated = True
                break
            if row.get('RowKey'):
                self.rows[str(row['RowKey'])] = row
        super().__init__(self.rows)

    @property
    def has_more(self):
        return self.truncated

    def subset(self, names):
        return DirtySnapshot((self.rows[name] for name in names if name in self.rows), truncated=self.truncated)


def read_bounded_dirty(reader, *args, max_rows):
    """Call a capped API, supporting legacy adapters without retrying internal TypeErrors."""
    import inspect
    signature = inspect.signature(reader)
    try:
        signature.bind(*args, max_rows=max_rows)
    except TypeError:
        result = reader(*args)
    else:
        result = reader(*args, max_rows=max_rows)
    if result is None or not isinstance(result, (set, list, tuple)):
        return result
    incomplete = bool(getattr(result, 'has_more', False) or getattr(result, 'truncated', False))
    if isinstance(result, DirtySnapshot):
        return DirtySnapshot(result.rows.values(), max_rows=max_rows, truncated=incomplete)
    if len(result) > max_rows or incomplete:
        # Legacy collections have no row versions; never bless a partial
        # capture as complete, including an empty truncated capture.
        return DirtySnapshot(({'RowKey': name} for name in result), max_rows=max_rows, truncated=True)
    return result


def acknowledge_dirty(table, snapshot):
    """Delete only the versions read before publication; never reread-and-clear."""
    if not isinstance(snapshot, DirtySnapshot):
        raise TypeError('Dirty acknowledgement requires the original snapshot')
    rows = list(snapshot.rows.values())
    for row in rows:
        if not etag(row):
            raise RuntimeError('Dirty row has no ETag')
    submit = getattr(table, 'submit_transaction', None)
    for start in range(0, len(rows), 100):
        chunk = rows[start:start + 100]
        if submit is not None:
            try:
                submit([('delete', row, {'etag': etag(row),
                        'match_condition': MatchConditions.IfNotModified}) for row in chunk])
                continue
            except TableTransactionError as error:
                if error.status_code not in (404, 412):
                    raise
            except (ResourceModifiedError, ResourceNotFoundError):
                pass
        for row in chunk:
            _acknowledge_one(table, row)


def _acknowledge_one(table, row):
    version = etag(row)
    try:
        table.delete_entity(row['PartitionKey'], row['RowKey'], etag=version,
                            match_condition=MatchConditions.IfNotModified)
    except (ResourceModifiedError, ResourceNotFoundError):
        pass  # Re-marked during the build, or already acknowledged.


class WakeupContention(RuntimeError):
    """notify() couldn't win the reservation write after every retry -- this
    row is under heavy concurrent write pressure, not a send() failure. A
    distinct type so callers that want to tell contention apart from an
    ordinary advisory-send failure (e.g. for metrics) can, without recover()
    having to abort its batch over it -- skipping and retrying next pass is
    correct either way, since every step here is idempotent."""


class WorkLeased(RuntimeError):
    """Another worker owns this intent; a duplicate delivery is not a failure."""


@dataclass(frozen=True)
class QueueWakeup:
    """Accepted queue delivery and its best-effort cancellation adapter."""
    queue_name: str
    message_id: str
    pop_receipt: str
    cancel: Callable[[dict], None]

    @property
    def receipt(self):
        return dict(queueName=self.queue_name, messageId=self.message_id, popReceipt=self.pop_receipt)

    def cancel_receipt(self, receipt):
        if receipt:
            try:
                self.cancel(receipt)
            except Exception:
                logging.getLogger(__name__).warning(
                    'Superseded index wake cancellation failed; receipt remains safe to consume')


def _wake_receipt(row):
    try:
        receipt = json.loads(row.get('wakeReceipt') or '{}')
        if isinstance(receipt, dict) and all(isinstance(receipt.get(k), str) and receipt[k]
                for k in ('queueName', 'messageId', 'popReceipt')):
            return receipt
    except (ValueError, TypeError):
        pass
    return None


@dataclass(frozen=True)
class Claim:
    key: str
    library: str
    scope: str
    revision: str
    owner: str
    attempt: int
    scheduled_at: float = 0
    scheduled_delay: float = 0
    schedule_id: str = ''
    schedule_reason: str = ''


def _retry_timing(row):
    """Optional telemetry must never stop a work claim on legacy/bad fields."""
    try:
        if row.get('retryScheduleRevision') != row['revision']:
            return (0, 0, '', '')
        scheduled = float(row.get('retryScheduledAt') or 0)
        delay = float(row.get('retryDelaySeconds') or 0)
        identity = row.get('retryScheduleId') or ''
        if (not math.isfinite(scheduled) or not math.isfinite(delay) or scheduled < 0 or delay < 0
                or not isinstance(identity, str) or len(identity) != 32
                or any(char not in '0123456789abcdef' for char in identity)):
            return (0, 0, '', '')
        reason = row.get('retryReason')
        return (scheduled, delay, identity, reason if reason in {
            'progress', 'barrier_wait', 'wave_wait', 'coordination_conflict', 'continuation'} else 'other')
    except (ValueError, TypeError):
        return (0, 0, '', '')


class WorkStore:
    def __init__(self, table, *, clock=time.time, lease_seconds=180, wake_seconds=60, partition=PARTITION):
        if not isinstance(partition, str) or not partition or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in partition):
            raise ValueError('Invalid work partition')
        self.partition = partition
        self.table = table
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.wake_seconds = wake_seconds

    @staticmethod
    def key(library, scope):
        return hashlib.sha256(f'{library}\0{scope}'.encode()).hexdigest()

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def _replace(self, current, row):
        if current is None:
            self.table.create_entity(row)
        else:
            version = etag(current)
            if not version:
                raise RuntimeError('Maintenance intent has no ETag')
            self.table.update_entity(row, mode=UpdateMode.REPLACE, etag=version,
                                     match_condition=MatchConditions.IfNotModified)

    def request(self, library, scope, *, advance=True, delay=0):
        key = self.key(library, scope)
        revision = str(uuid.uuid4())
        for _ in range(12):
            current = self._get(key)
            if current is not None and not advance:
                return key
            row = dict(current or {}, PartitionKey=self.partition, RowKey=key,
                       libraryId=library, scope=scope, revision=revision,
                       requestedAt=self.clock(), failed=False)
            row.setdefault('leaseUntil', 0.0)
            row.setdefault('wakeUntil', 0.0)
            target = self.clock() + max(0, delay)
            if current and float(current.get('leaseUntil') or 0) <= self.clock():
                target = min(float(current.get('notBefore') or target), target)
            if current and not current.get('failed') and current.get('retryReason') in ('barrier_wait', 'wave_wait'):
                # Source/job revisions do not prove the guarded wave is clear.
                # Keep its embargo even when a writer advances the revision.
                target = max(float(current.get('notBefore') or 0), target)
            row['notBefore'] = target
            if current and current.get('failed'):
                row.update(owner='', leaseUntil=0, wakeUntil=0)
            try:
                self._replace(current, row)
                return key
            except (ResourceExistsError, ResourceModifiedError):
                continue
        raise RuntimeError('Maintenance request contention')

    def notify(self, key, send, *, minimum_delay=0):
        """Reserve a wakeup before sending; recovery retries expired reservations."""
        for _ in range(12):
            current = self._get(key)
            if current is None or current.get('failed'):
                return False
            now = self.clock()
            if float(current.get('leaseUntil') or 0) > now or float(current.get('wakeUntil') or 0) > now:
                return False
            delay = max(0, math.ceil(float(current.get('notBefore') or 0) - now),
                        math.ceil(minimum_delay))
            reservation = uuid.uuid4().hex
            previous_receipt = _wake_receipt(current)
            row = dict(current, wakeUntil=now + delay + self.wake_seconds,
                       wakeReservationId=reservation)
            try:
                self._replace(current, row)
            except ResourceModifiedError:
                continue
            # On failure the durable reservation expires; do not delete intent.
            published = send(row['libraryId'], row['scope'], row['revision'], delay)
            if isinstance(published, QueueWakeup):
                self._record_wakeup(key, reservation, published, delayed=delay > 0,
                                    previous_receipt=previous_receipt)
            return True
        raise WakeupContention('Maintenance wakeup contention')

    def _record_wakeup(self, key, reservation, published, *, delayed, previous_receipt):
        try:
            for _ in range(12):
                current = self._get(key)
                if current is None:
                    # A fast replacement already finished before this send returned.
                    published.cancel_receipt(published.receipt)
                    return
                if current.get('wakeReservationId') != reservation:
                    # A racing owner may be an inline builder without another
                    # receipt. Attach its fallback without changing ownership,
                    # but never overwrite a newer recorded queue delivery.
                    if not delayed:
                        return
                    if _wake_receipt(current) not in (None, previous_receipt):
                        # A newer delayed send already supplied the recovery
                        # receipt. This late response must cancel its own
                        # delivery instead of leaving an untracked wake behind.
                        published.cancel_receipt(published.receipt)
                        return
                try:
                    if delayed:
                        displaced_receipt = _wake_receipt(current)
                        self._replace(current, dict(current, wakeReceipt=json.dumps(published.receipt)))
                        # The ETag certifies which receipt this write displaced.
                        # Another response may have attached it during send(),
                        # so the pre-send snapshot is insufficient for cleanup.
                        if displaced_receipt != published.receipt:
                            published.cancel_receipt(displaced_receipt)
                    # Immediate deliveries can be deferred/ACKed early; retain
                    # their delayed fallback until finish certifies completion.
                    return
                except (ResourceModifiedError, ResourceNotFoundError):
                    continue
        except Exception:
            # An accepted delivery remains valid even if recording its handle
            # fails. Never turn a bookkeeping outage into another queue send.
            logging.getLogger(__name__).warning('Index wake receipt could not be retained')

    def claim(self, key):
        for _ in range(12):
            current = self._get(key)
            if current is None or current.get('failed'):
                return None
            now = self.clock()
            if float(current.get('leaseUntil') or 0) > now:
                raise WorkLeased('Maintenance work is already leased')
            if float(current.get('notBefore') or 0) > now:
                # Queue messages are advisory, including stale duplicates.
                # Do not consume attempts or clear the reserved delayed wake.
                return None
            owner = str(uuid.uuid4())
            attempt = (int(current.get('attempts') or 0) + 1
                       if current.get('attemptRevision') == current['revision'] else 1)
            row = dict(current, owner=owner, leaseUntil=now + self.lease_seconds, wakeUntil=0,
                       attempts=attempt, attemptRevision=current['revision'],
                       wakeReservationId='')
            timing = _retry_timing(current)
            row.update(retryScheduleId='', retryScheduledAt=0)
            try:
                self._replace(current, row)
                return Claim(key, row['libraryId'], row['scope'], row['revision'], owner, attempt,
                    *timing)
            except ResourceModifiedError:
                continue
        raise RuntimeError('Maintenance claim contention')

    def renew(self, claim):
        for _ in range(12):
            current = self._get(claim.key)
            if (current is None or current.get('owner') != claim.owner
                    or float(current.get('leaseUntil') or 0) <= self.clock()):
                raise RuntimeError('Maintenance lease lost')
            try:
                self._replace(current, dict(current, leaseUntil=self.clock() + self.lease_seconds))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Maintenance lease renewal contention')

    def release(self, claim, *, deferred=False, retry_delay=None, retry_reason='continuation', reset_attempts=False):
        if reset_attempts and not deferred:
            raise ValueError('Only verified progress may reset deferred work attempts')
        for _ in range(12):
            current = self._get(claim.key)
            if current is None or current.get('owner') != claim.owner:
                raise RuntimeError('Maintenance lease lost')
            try:
                row = dict(current, owner='', leaseUntil=0,
                           wakeUntil=self.clock() + self.lease_seconds)
                if retry_delay is not None:
                    # Explicit continuation: notify reserves and sends the next
                    # delayed wake before the current queue message is ACKed.
                    scheduled_at = self.clock()
                    row.update(wakeUntil=0, notBefore=scheduled_at + max(0, retry_delay),
                        retryScheduledAt=scheduled_at, retryDelaySeconds=max(0, retry_delay),
                        retryScheduleId=uuid.uuid4().hex, retryScheduleRevision=claim.revision,
                        retryReason=retry_reason)
                # Barrier waits refund this claim; verified durable progress
                # additionally clears failures accumulated on earlier pages.
                if deferred and current['revision'] == claim.revision:
                    row['attempts'] = (0 if reset_attempts else
                                       max(0, int(current.get('attempts') or 0) - 1))
                self._replace(current, row)
                if retry_delay is not None:
                    import processing_diagnostics
                    parts = claim.scope.split(':')
                    if len(parts) == 3 and parts[0] in ('wave-vision', 'wave-cluster', 'wave-index'):
                        processing_diagnostics.emit('wave_retry_scheduled', wave_id=parts[1],
                            stage=parts[0][5:], schedule_id=row['retryScheduleId'],
                            attempt_id=claim.owner,
                            reason=retry_reason, requested_delay_seconds=max(0, retry_delay),
                            scheduled_at=scheduled_at, dispatch_attempt=claim.attempt)
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Maintenance release contention')

    def fail_revision(self, key, revision):
        for _ in range(12):
            current = self._get(key)
            if current is None or current['revision'] != revision:
                return
            try:
                self._replace(current, dict(current, owner='', leaseUntil=0, wakeUntil=0, failed=True, failedAt=self.clock()))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Maintenance failure contention')

    def finish(self, claim, *, failed=False, cancel_wakeup=None):
        """Return True when a newer revision must be scheduled after this run."""
        for _ in range(12):
            current = self._get(claim.key)
            if (current is None or current.get('owner') != claim.owner
                    or float(current.get('leaseUntil') or 0) <= self.clock()):
                raise RuntimeError('Maintenance lease lost')
            newer = current['revision'] != claim.revision
            try:
                if not failed and not newer:
                    self.table.delete_entity(self.partition, claim.key, etag=etag(current),
                                             match_condition=MatchConditions.IfNotModified)
                    receipt = _wake_receipt(current)
                    if receipt and cancel_wakeup is not None:
                        # Commit first. Cancellation failure must never resurrect
                        # completed work or discard a newer revision's recovery.
                        try:
                            cancel_wakeup(receipt)
                        except Exception:
                            logging.getLogger(__name__).warning(
                                'Completed index wake cancellation failed; receipt remains safe to consume')
                else:
                    self._replace(current, dict(current, owner='', leaseUntil=0, wakeUntil=0,
                                                failed=bool(failed and not newer), failedAt=self.clock() if failed and not newer else 0))
                return newer
            except (ResourceModifiedError, ResourceNotFoundError):
                continue
        raise RuntimeError('Maintenance completion contention')

    def recover(self, send, *, limit=100, library=None):
        query = f"PartitionKey eq '{self.partition}' and failed eq false"
        if library is not None:
            query += " and libraryId eq '" + library.replace("'", "''") + "'"
        # Exclude reserved work server-side before applying the bounded page.
        # Otherwise the same first 100 leased rows starve every later library.
        now = float(self.clock())
        query += f' and leaseUntil le {now} and wakeUntil le {now}'
        import itertools
        sent = 0
        for row in itertools.islice(self.table.query_entities(query), limit):
            # One row's send() failing (e.g. a stage's queue client
            # unavailable) must not abort the whole batch -- the untouched
            # row stays eligible and a later recover() call retries it,
            # same as any other advisory wake in this module. Contention on
            # the reservation write itself (WakeupContention) is handled the
            # same way -- skip and let a later pass retry -- but kept as a
            # distinct, nameable type rather than conflated with a generic
            # send() failure, so a caller that wants to tell them apart (e.g.
            # for metrics on a hot row) can.
            try:
                sent += int(self.notify(row['RowKey'], send))
            except WakeupContention:
                continue
            except Exception:
                continue
        return sent

    def resume_wave_waits(self, library, send, *, scopes):
        """A terminal wave can release its own embargo, never a live claim.

        The old delayed receipt stays available until finish certifies the intent.
        Failed sends retain that receipt and an expiring reservation.
        """
        query = (f"PartitionKey eq '{self.partition}' and libraryId eq '"
                 + library.replace("'", "''") + "'")
        resumed = 0
        for candidate in self.table.query_entities(query):
            if candidate.get('scope') not in scopes:
                continue
            for _ in range(12):
                row = self._get(candidate['RowKey'])
                if (not row or row.get('libraryId') != library or row.get('scope') not in scopes
                        or row.get('failed') or row.get('retryReason') != 'wave_wait'
                        or float(row.get('leaseUntil') or 0) > self.clock()):
                    break
                try:
                    self._replace(row, dict(row, notBefore=0, wakeUntil=0,
                        retryReason='', retryScheduleId='', retryScheduledAt=0))
                except ResourceModifiedError:
                    continue
                resumed += int(self.notify(row['RowKey'], send))
                break
        return resumed
