"""Bounded Vision coordination batches; source artifacts retain their own fences."""
from urllib.parse import quote as url_quote
from collections import OrderedDict
from azure.core.exceptions import ResourceNotFoundError

from upload_waves import Conflict, MAX_PAGE, digest
from processing_receipts import ArtifactReceipts
from processing_metrics import add


class ReceiptBatch(ArtifactReceipts):
    """Reuse immutable artifact rows while still validating every reference.

    Mutable metadata, inboxes, policies and leases are never cached. Missing
    rows are not retained: source processing can publish them during the page.
    """
    def __init__(self, table, library):
        super().__init__(table, library)
        self.rows = OrderedDict()

    def _remember(self, key, row):
        if row is not None and key.startswith(('manifest!', 'face!')):
            self.rows[key] = row
            self.rows.move_to_end(key)
            while len(self.rows) > 4096:
                self.rows.popitem(last=False)
                add(receipt_cache_evictions=1)

    def prefetch(self, keys):
        from table_reads import read_entities, key_batches
        for batch in key_batches(key for key in keys if key not in self.rows):
            for key, row in read_entities(self.table, self.partition, batch).items():
                self._remember(key, row)

    def _get(self, key):
        if key in self.rows:
            add(receipt_cache_hits=1)
            self.rows.move_to_end(key)
            return self.rows[key]
        row = super()._get(key)
        add(receipt_cache_misses=1)
        self._remember(key, row)
        return row


class MetadataBatch:
    """Bounded metadata queries, invalidated before processing writes.

    These snapshots only select work. Source claims and result commits still
    reread the authoritative metadata table under their generation fences.
    """
    def __init__(self, table, library, members):
        self.table, self.library, self.rows = table, library, {}
        if len(members) > MAX_PAGE:
            raise ValueError('Metadata batch exceeds member page')
        names = {member['filename'] for member in members}
        if hasattr(table, 'query_entities') and names:
            quote = lambda value: value.replace("'", "''")
            prefix = "PartitionKey eq '" + quote(library) + "' and ("
            filters = []
            def fetch():
                query = prefix + ' or '.join(filters) + ')'
                self.rows.update({row['RowKey']: row for row in
                    table.query_entities(query, results_per_page=MAX_PAGE) if row.get('RowKey') in names})
            for name in sorted(names):
                clause = "RowKey eq '" + quote(name) + "'"
                # One PK comparison plus at most fourteen row comparisons;
                # also bound the encoded filter for long/non-ASCII filenames.
                candidate = prefix + ' or '.join([*filters, clause]) + ')'
                if filters and (len(filters) == 14 or len(url_quote(candidate)) > 6000):
                    fetch()
                    filters = []
                filters.append(clause)
            if filters:
                fetch()
            for name in names:
                self.rows.setdefault(name, None)

    def get_entity(self, library, filename):
        if library == self.library and filename in self.rows:
            row = self.rows[filename]
            if row is None:
                raise ResourceNotFoundError('Source absent from metadata batch')
            return dict(row)
        return self.table.get_entity(library, filename)

    def invalidate(self, filename):
        self.rows.pop(filename, None)

    def __getattr__(self, name):
        return getattr(self.table, name)


def reserve_attempts(store, members):
    """Commit a bounded group's attempt admissions together, before inference.

    A crash after admission consumes that attempt; it must not silently grant
    another model run. Qualified results are checked before reaching this path.
    """
    from execution_lease import guard_operations
    from wave_settlement import MAX_SOURCE_ATTEMPTS
    if not members or len(members) > MAX_PAGE:
        raise ValueError('Expected a bounded source group')
    first = [True]
    def commit():
        read_existing = not first[0]
        first[0] = False
        changes, counts = [], {}
        for member in members:
            key = 'source-attempt!' + digest([member['waveId'], member['photoId'], member['processingRevision']])
            # A create-only transaction proves first admission without a
            # point read for every new photo. On conflict the entire group
            # rolls back; reread existing counters before retrying the CAS.
            old = store._get(key) if read_existing else None
            previous = int((old or {}).get('count', 0))
            admitted = previous < MAX_SOURCE_ATTEMPTS
            count = previous + int(admitted)
            counts[member['RowKey']] = (count, admitted)
            if admitted:
                changes.append(store._change(old, store._row(key, count=count, updatedAt=store.clock())))
        if changes:
            store.table.submit_transaction([*changes, *store._policy_guard(), *guard_operations(store)])
        return counts
    return store._retry(commit)


def commit_observations(store, wave_id, observations):
    """One control/wave/ownership transaction for the page's qualified results."""
    from wave_settlement import verified
    if len(observations) > MAX_PAGE:
        raise ValueError('Observation batch exceeds member page')
    if not observations:
        return
    first = [True]
    def commit():
        read_current = not first[0]
        first[0] = False
        control = store._required('control')
        wave = store._required(store.wave_key(wave_id))
        changes = []
        for member, completion, leased in observations:
            # The member page already supplied an ETag. Reuse it on the first
            # transaction; any intervening mutation rejects the whole batch.
            old = store._required(member['RowKey']) if read_current else member
            if old['outcome'] in ('deleted', 'superseded', 'failed_exhausted', 'excluded') and verified(store, old):
                continue
            fields = dict(outcome='settled' if completion else 'pending',
                evidenceId=completion[0]['id'] if completion else digest(['pending', member['photoId'], member['processingRevision']]),
                evidenceQualified=bool(completion), outstandingLeases=int(leased and completion is None))
            if all(old.get(key) == value for key, value in fields.items()):
                continue
            if wave['state'] in ('ready', 'indexing', 'complete'):
                raise Conflict('Frozen wave evidence cannot change')
            changes.append(store._change(old, dict(old, **fields, observedAt=store.clock())))
        if changes:
            changes.append(store._change(wave, dict(wave, lastProgressAt=store.clock(), updatedAt=store.clock())))
            store._commit(control, changes)
    store._retry(commit)
