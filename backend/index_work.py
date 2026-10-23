"""Durable maintenance intents, independent of user-visible job status.

Queue messages are wakeups. A coalesced request still advances the revision,
so completing an earlier revision cannot consume edits received during a run.
The pending partition is also the recovery index for failed queue publication.
"""
from __future__ import annotations

import hashlib
import math
import time
import uuid
from dataclasses import dataclass

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableTransactionError, UpdateMode

PARTITION = 'index-maintenance-pending'


def etag(row):
    return getattr(row, 'metadata', {}).get('etag')


class DirtySnapshot(set):
    def __init__(self, rows=()):
        self.rows = {str(row['RowKey']): row for row in rows if row.get('RowKey')}
        super().__init__(self.rows)

    def subset(self, names):
        return DirtySnapshot(self.rows[name] for name in names if name in self.rows)


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


@dataclass(frozen=True)
class Claim:
    key: str
    library: str
    scope: str
    revision: str
    owner: str
    attempt: int


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
            row['notBefore'] = target
            if current and current.get('failed'):
                row.update(owner='', leaseUntil=0, wakeUntil=0)
            try:
                self._replace(current, row)
                return key
            except (ResourceExistsError, ResourceModifiedError):
                continue
        raise RuntimeError('Maintenance request contention')

    def notify(self, key, send):
        """Reserve a wakeup before sending; recovery retries expired reservations."""
        for _ in range(12):
            current = self._get(key)
            if current is None or current.get('failed'):
                return False
            now = self.clock()
            if float(current.get('leaseUntil') or 0) > now or float(current.get('wakeUntil') or 0) > now:
                return False
            delay = max(0, math.ceil(float(current.get('notBefore') or 0) - now))
            row = dict(current, wakeUntil=now + delay + self.wake_seconds)
            try:
                self._replace(current, row)
            except ResourceModifiedError:
                continue
            # On failure the durable reservation expires; do not delete intent.
            send(row['libraryId'], row['scope'], row['revision'], delay)
            return True
        raise RuntimeError('Maintenance wakeup contention')

    def claim(self, key):
        for _ in range(12):
            current = self._get(key)
            if current is None or current.get('failed'):
                return None
            now = self.clock()
            if float(current.get('leaseUntil') or 0) > now:
                raise RuntimeError('Maintenance work is already leased')
            owner = str(uuid.uuid4())
            attempt = (int(current.get('attempts') or 0) + 1
                       if current.get('attemptRevision') == current['revision'] else 1)
            row = dict(current, owner=owner, leaseUntil=now + self.lease_seconds, wakeUntil=0,
                       attempts=attempt, attemptRevision=current['revision'])
            try:
                self._replace(current, row)
                return Claim(key, row['libraryId'], row['scope'], row['revision'], owner, attempt)
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

    def release(self, claim):
        for _ in range(12):
            current = self._get(claim.key)
            if current is None or current.get('owner') != claim.owner:
                raise RuntimeError('Maintenance lease lost')
            try:
                self._replace(current, dict(current, owner='', leaseUntil=0,
                                            wakeUntil=self.clock() + self.lease_seconds))
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
                self._replace(current, dict(current, owner='', leaseUntil=0, wakeUntil=0, failed=True))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Maintenance failure contention')

    def finish(self, claim, *, failed=False):
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
                else:
                    self._replace(current, dict(current, owner='', leaseUntil=0, wakeUntil=0,
                                                failed=bool(failed and not newer)))
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
            sent += int(self.notify(row['RowKey'], send))
        return sent
