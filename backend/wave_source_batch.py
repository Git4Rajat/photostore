"""Bounded Vision coordination batches; source artifacts retain their own fences."""
from azure.core.exceptions import ResourceNotFoundError

from upload_waves import Conflict, MAX_PAGE, digest
from processing_receipts import ArtifactReceipts
from processing_metrics import add, timer


class ReceiptBatch(ArtifactReceipts):
    """Reuse immutable artifact rows across pages within one Vision attempt.

    Mutable metadata, inboxes, policies and leases are never cached. Missing
    rows are not retained: source processing can publish them during the page.
    """
    def __init__(self, table, library):
        from vision_buffer import LocalRows, limit_bytes
        super().__init__(table, library)
        self.rows = LocalRows(prefix='receipt_cache',
            memory_bytes=limit_bytes('IPWORK_VISION_RECEIPT_MEMORY_MB', 8, 64),
            disk_bytes=limit_bytes('IPWORK_VISION_RECEIPT_DISK_MB', 256, 1024))
        self.inspection = None

    def _remember(self, key, row):
        if row is not None and key.startswith(('manifest!', 'face!')):
            while not self.rows.put(key, row):
                if not len(self.rows):
                    add(receipt_cache_oversize=1)
                    break
                self.rows.pop(next(iter(self.rows.entries)))
                add(receipt_cache_evictions=1)

    def prefetch(self, keys):
        from table_reads import read_entities
        pending = list(dict.fromkeys(key for key in keys if key not in self.rows))
        for key, row in read_entities(self.table, self.partition, pending, workers=4, group_namespaces=True).items():
            self._remember(key, row)
        self.rows.flush()

    def _get(self, key):
        if key in self.rows:
            add(receipt_cache_hits=1)
            try:
                return self.rows.get(key)
            except KeyError:
                self.rows.pop(key)
        row = super()._get(key)
        add(receipt_cache_misses=1)
        self._remember(key, row)
        return row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.rows.clear()


class InspectionBatch:
    """Page-local selection snapshots; never used to authorize source writes.

    Only immutable receipt payloads survive this page. Rebuild mutable
    inbox/proof snapshots after processing; final qualification still reads
    authoritative metadata rather than the snapshot used to plan these reads.
    """
    def __init__(self, store, members, metadata, receipts):
        import json
        from table_reads import read_entities
        from wave_settlement import deletion_key
        self.inboxes = {}
        proof_keys, inbox_keys = [], []
        for member in members:
            photo, revision = member['photoId'], member['processingRevision']
            proof_keys.extend([deletion_key(photo, revision), 'supersession!' + digest([photo, revision])])
            if member['outcome'] not in ('pending', 'settled') and member.get('evidenceId'):
                proof_keys.append('settlement!' + member['evidenceId'])
            try:
                row = metadata.get_entity(store.library, member['filename'])
            except ResourceNotFoundError:
                continue
            if (row.get('anonymousImageId') and row.get('processingRevision')
                    and (row['anonymousImageId'], row['processingRevision']) != (photo, revision)):
                proof_keys.append('admitted-source!' + digest([row['anonymousImageId'], row['processingRevision']]))
            if row.get('processingCompletionId'):
                inbox_keys.append('source-completion!' + digest([photo, revision]) + '!' + row['processingCompletionId'])
        with timer('vision_proof_inbox_fetch', count_name='vision_proof_inbox_fetch_calls'):
            rows = read_entities(store.table, store.partition, [*proof_keys, *inbox_keys], workers=4, group_namespaces=True)
        self.proofs = {key: rows[key] for key in proof_keys}
        self.inboxes = {key: rows[key] for key in inbox_keys}
        references = []
        for inbox in self.inboxes.values():
            if inbox is not None and inbox.get('artifactReceiptQualified'):
                try:
                    reference = json.loads(inbox['payload']).get('artifactReceipt')
                    if isinstance(reference, dict) and isinstance(reference.get('id'), str):
                        references.append(reference)
                except (ValueError, TypeError):
                    pass  # Per-member validation still reports corrupt evidence.
        with timer('vision_manifest_fetch', count_name='vision_manifest_fetch_calls'):
            receipts.prefetch('manifest!' + ref['id'] for ref in references)
        faces = []
        for ref in references:
            manifest = receipts._get('manifest!' + ref['id'])
            if manifest is not None:
                try:
                    faces.extend('face!' + key for key in json.loads(manifest['payload'])['faceSnapshots'])
                except (ValueError, TypeError, KeyError):
                    pass
        with timer('vision_face_receipt_fetch', count_name='vision_face_receipt_fetch_calls'):
            receipts.prefetch(faces)


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
            from table_reads import read_entities
            self.rows = read_entities(table, library, names, workers=4)
            from vision_buffer import encode
            from processing_metrics import observe
            observe(vision_metadata_rows_peak=len(self.rows), vision_metadata_snapshot_peak_bytes=sum(
                len(encode(row)) for row in self.rows.values() if row is not None))

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
