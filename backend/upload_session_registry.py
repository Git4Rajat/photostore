"""Upload lifecycle directory and notices, reconciled only on Core traffic.

The wave session is the authoritative activity/fencing record. This separate
table is written at admission and settlement, never on every heartbeat/file.
All cross-table steps are replayable: retain the directory entry until stage
handoff and (for inactivity) durable notification publication have succeeded.
"""
import time
import uuid
from itertools import islice

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode, TableTransactionError

from upload_waves import Conflict, _etag, digest, identifier

CLAIM_SECONDS = 60
STALE_SECONDS = 300
NOTICE_RETENTION_SECONDS = 14 * 86400
PAGE_SIZE = 20


def configured():
    import app
    if app.upload_sessions_table_client is None:
        raise AzureError('Upload session directory unavailable')
    return app.upload_sessions_table_client


class Sessions:
    def __init__(self, table, library, *, clock=time.time):
        self.table, self.library, self.clock = table, library, clock
        self.partition = 'library-' + digest(library)

    def get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def change(self, old, row):
        return ('update', row, dict(mode=UpdateMode.REPLACE, etag=_etag(old),
                                    match_condition=MatchConditions.IfNotModified))

    def delete(self, row):
        return ('delete', dict(PartitionKey=self.partition, RowKey=row['RowKey']),
                dict(etag=_etag(row), match_condition=MatchConditions.IfNotModified))

    def page(self, prefix, after='', size=PAGE_SIZE, extra_filter=''):
        query = (f"PartitionKey eq '{self.partition}' and RowKey ge '{prefix}' "
                 f"and RowKey lt '{prefix}~'")
        if after:
            query += f" and RowKey gt '{after}'"
        if extra_filter:
            query += ' and ' + extra_filter
        return list(islice(self.table.query_entities(query, results_per_page=size), size))

    def track(self, session, *, created_at=None):
        """Write before wave creation. A crash cannot leave unindexed intake."""
        key = 'session!' + identifier(session)
        row = dict(PartitionKey=self.partition, RowKey=key, sessionId=session,
                   libraryId=self.library, state='OPEN',
                   createdAt=self.clock() if created_at is None else created_at,
                   claimUntil=0, claimToken='')
        try:
            self.table.submit_transaction([('create', row)])
        except ResourceExistsError:
            pass
        except TableTransactionError as error:
            if error.status_code != 409:
                raise

    def forget(self, session):
        row = self.get('session!' + identifier(session))
        if row:
            self.table.submit_transaction([self.delete(row)])

    def notices(self):
        result = []
        # Bound retention cleanup to actual visits. Read receipts prevent an
        # old migration survey from recreating a dismissed notice.
        for row in self.page('notice!', extra_filter=f'expiresAt le {self.clock()}'):
            self.table.submit_transaction([self.delete(row)])
        for row in self.page('notice!', size=100, extra_filter=f'read eq false and expiresAt gt {self.clock()}'):
            result.append({key: row[key] for key in (
                'sessionId', 'waveId', 'affectedCount', 'closedAt')})
        return result

    def acknowledge(self, session):
        row = self.get('notice!' + identifier(session))
        if row and not row.get('read'):
            self.table.submit_transaction([self.change(row, dict(row, read=True))])

    def settle(self, entry, parent, drive):
        if parent and not parent.get('discardAbsent'):
            if drive(parent['waveId']) is False:
                raise Conflict('Wave execution is paused; retain the upload recovery entry')
        # Publish the notice and remove the directory entry atomically. A failed
        # write leaves the entry available for the next user-triggered attempt.
        operations = [self.delete(entry)]
        if parent and parent.get('leaseExpiredAt'):
            notice = dict(PartitionKey=self.partition,
                RowKey='notice!' + parent['sessionId'], sessionId=parent['sessionId'],
                waveId=parent['waveId'], closedAt=parent['closedAt'],
                affectedCount=max(0, max(parent.get('abandonUnfinishedCount', 0),
                    parent.get('abandonCancelledCount', 0) + parent.get('failedCount', 0) + parent.get('cancelledCount', 0)) +
                    parent['declaredCount'] - parent['registeredCount']), read=False,
                expiresAt=self.clock() + NOTICE_RETENTION_SECONDS)
            # An older migration survey can replay after settlement. Preserve
            # the original notice/read receipt rather than notifying twice.
            if self.get(notice['RowKey']) is None:
                operations.append(('create', notice))
        self.table.submit_transaction(operations)
        import processing_diagnostics
        processing_diagnostics.emit('upload_session_reconciled', library_id=digest(self.library),
            session_id=digest(entry['sessionId']), wave_id=(parent or {}).get('waveId', ''),
            stale=bool((parent or {}).get('leaseExpiredAt')),
            cancelled=(parent or {}).get('abandonCancelledCount', 0))

    def reconcile_one(self, store, entry, drive):
        if entry.get('claimUntil', 0) > self.clock():
            return True
        session = entry['sessionId']
        parent = store._get(store.session_key(session))
        if parent and parent['state'] == 'open' and not parent.get('abandoning'):
            deadline = parent.get('leaseExpiresAt') or (
                max(parent.get('lastRequestAt', 0), parent.get('heartbeatAt', 0),
                    parent.get('createdAt', 0)) + STALE_SECONDS)
            if deadline > self.clock():
                return False
        elif not parent and entry['createdAt'] + STALE_SECONDS > self.clock():
            return False  # Admission may still be committing in Upload.
        claimed = dict(entry, state='STOPPING', claimUntil=self.clock() + CLAIM_SECONDS,
                       claimToken=uuid.uuid4().hex)
        self.table.submit_transaction([self.change(entry, claimed)])
        entry = self.get(entry['RowKey'])
        if not entry or entry.get('claimToken') != claimed['claimToken']:
            return True
        settled = False
        try:
            if parent is None:
                from wave_settlement import fence_discard
                parent = fence_discard(store, session, reason='Upload admission interrupted', absent_only=True)
                if parent['state'] == 'open':
                    from upload_session_lease import seal_expired
                    parent = seal_expired(store, session)
            else:
                from upload_session_lease import seal_expired
                # Re-read/CAS against the session itself. A heartbeat, register,
                # or finalize winning this race keeps the intake alive.
                parent = seal_expired(store, session)
            if parent and parent['state'] == 'open' and not parent.get('abandoning'):
                return False
            if parent and parent['state'] == 'open':
                from wave_settlement import abandon
                for _ in range(20):
                    parent = abandon(store, session, parent['manifestRevision'],
                                     reason=parent.get('abandonReason', 'Upload inactive'))
                    if parent['state'] != 'open':
                        break
                if parent['state'] == 'open':
                    return True
            self.settle(entry, parent, drive)
            settled = True
            return False
        finally:
            # Release only our claim; an expired owner cannot overwrite a newer
            # replica's work. Failed releases simply expire for the next visit.
            current = None if settled else self.get(entry['RowKey'])
            if current and current.get('claimToken') == claimed['claimToken']:
                state = 'OPEN' if parent and parent['state'] == 'open' and not parent.get('abandoning') else 'STOPPING'
                self.table.submit_transaction([self.change(current, dict(current, state=state, claimUntil=0, claimToken=''))])

    def reconcile(self, store, drive, *, budget_seconds=2):
        started = time.monotonic()
        cursor = self.get('survey')
        after = (cursor or {}).get('after', '')
        rows = self.page('session!', after)
        pending = False
        for row in rows:
            if time.monotonic() - started >= budget_seconds:
                pending = True
                break
            try:
                pending = self.reconcile_one(store, row, drive) or pending
            except (Conflict, ResourceModifiedError, ResourceExistsError, TableTransactionError) as error:
                import processing_diagnostics
                processing_diagnostics.emit('upload_session_reconcile_deferred',
                    library_id=digest(self.library), session_id=digest(row['sessionId']),
                    reason=type(error).__name__)
                pending = True
            after = row['RowKey']
        if not rows or (len(rows) < PAGE_SIZE and (not rows or after == rows[-1]['RowKey'])):
            after = ''
        else:
            pending = True
        survey = dict(PartitionKey=self.partition, RowKey='survey', after=after)
        try:
            if not cursor or cursor.get('after') != after:
                self.table.submit_transaction([self.change(cursor, survey) if cursor else ('create', survey)])
        except (ResourceModifiedError, ResourceExistsError, TableTransactionError):
            pending = True
        return pending


def finish(store, parent):
    """Upload-side terminal hook. Preserve stale closures for Core's notice."""
    from upload_wave_execution import execution_enabled
    if (execution_enabled() and parent.get('lifecycleManaged')
            and parent['state'] != 'open' and not parent.get('leaseExpiredAt')):
        Sessions(configured(), store.library, clock=store.clock).forget(parent['sessionId'])
