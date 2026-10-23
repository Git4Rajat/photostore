import json

import pytest
from azure.core.exceptions import ResourceNotFoundError

from fakes import FakeTable
from processing_artifacts import staging_name
from processing_cleanup import (
    delete_orphaned_manifest, delete_staging_attempt, orphaned_manifest_candidates,
    protected_artifacts, referenced_manifest_ids, retained_manifest_rows,
)
from upload_waves import digest


class _Blob:
    def __init__(self, exists):
        self.exists = exists

    def delete_blob(self):
        if not self.exists:
            raise ResourceNotFoundError('missing')
        self.exists = False


class _Storage:
    def __init__(self):
        self._CTX = {'blob_thumbnail_container': 'thumbs'}
        self.existing = set()

    def seed(self, *names):
        self.existing.update(names)

    def _get_blob_client(self, container, name):
        return _Blob(name in self.existing)


def manifest_row(library, manifest_id, *, derivatives=None, face_snapshots=()):
    partition = 'processing-artifacts-' + digest(library)
    payload = dict(derivatives=derivatives or {}, faceSnapshots=list(face_snapshots))
    return dict(PartitionKey=partition, RowKey='manifest!' + manifest_id, payload=json.dumps(payload))


def test_delete_staging_attempt_refuses_the_currently_active_attempt():
    storage = _Storage()
    with pytest.raises(ValueError, match='currently active'):
        delete_staging_attempt(storage, 'lib', 'photo', 'rev', 'attempt-1', current_attempt='attempt-1')


def test_delete_staging_attempt_removes_an_abandoned_attempts_blobs():
    storage = _Storage()
    storage.seed(*(staging_name('lib', 'photo', 'rev', 'old-attempt', kind) for kind in ('thumbnail', 'preview')))
    deleted = delete_staging_attempt(storage, 'lib', 'photo', 'rev', 'old-attempt', current_attempt='new-attempt')
    assert deleted == 2


def test_delete_staging_attempt_tolerates_already_missing_blobs():
    storage = _Storage()  # Nothing seeded -- both blobs are already gone.
    deleted = delete_staging_attempt(storage, 'lib', 'photo', 'rev', 'old-attempt', current_attempt='new-attempt')
    assert deleted == 0


def test_delete_staging_attempt_is_a_noop_without_a_thumbnail_container():
    storage = _Storage()
    storage._CTX = {}
    assert delete_staging_attempt(storage, 'lib', 'photo', 'rev', 'attempt', current_attempt='other') == 0


def test_referenced_manifest_ids_collects_outbox_and_receipt_pointers_but_not_the_ack():
    rows = [
        # processingCompletionAck is a completion-INTENT id, a different
        # identity space entirely -- it must never be treated as a
        # manifest reference (that was a bug: see the module docstring).
        dict(processingCompletionAck='ack-id'),
        dict(processingCompletionOutbox=json.dumps([{'artifactReceipt': {'id': 'backlog-id'}}])),
        dict(artifactReceipt=json.dumps({'id': 'committed-id'})),
        dict(),  # No completion evidence at all -- contributes nothing.
    ]
    assert referenced_manifest_ids(rows) == {'backlog-id', 'committed-id'}


def test_referenced_manifest_ids_includes_durable_inbox_references():
    inbox_rows = [dict(payload=json.dumps({'artifactReceipt': {'id': 'inbox-id'}}))]
    assert referenced_manifest_ids([], inbox_rows) == {'inbox-id'}


@pytest.mark.parametrize('rows,inbox_rows', [
    ([dict(processingCompletionOutbox='not json')], []),
    ([dict(artifactReceipt='also not json')], []),
    ([], [dict(payload='not json either')]),
])
def test_referenced_manifest_ids_raises_on_corrupt_evidence_instead_of_undercounting(rows, inbox_rows):
    # Corrupt/incomplete evidence must block deletion, never be silently
    # skipped -- silently skipping it would undercount what's referenced
    # and could make a still-needed manifest look orphaned.
    with pytest.raises(ValueError, match='Corrupt'):
        referenced_manifest_ids(rows, inbox_rows)


def test_orphaned_and_retained_manifest_rows_partition_correctly():
    table = FakeTable()
    table.upsert_entity(manifest_row('lib', 'referenced'))
    table.upsert_entity(manifest_row('lib', 'orphaned'))
    assert [row['RowKey'] for row in orphaned_manifest_candidates(table, 'lib', {'referenced'})] == ['manifest!orphaned']
    assert [row['RowKey'] for row in retained_manifest_rows(table, 'lib', {'referenced'})] == ['manifest!referenced']


def test_protected_artifacts_aggregates_retained_blobs_and_snapshots():
    retained = [manifest_row('lib', 'kept', derivatives={'thumbnail': {'blob': 'shared.jpg'}},
                             face_snapshots=['shared-snap'])]
    blobs, snapshots = protected_artifacts(retained)
    assert blobs == {'shared.jpg'} and snapshots == {'shared-snap'}


def test_delete_orphaned_manifest_removes_blobs_snapshots_and_the_manifest_row():
    table = FakeTable()
    storage = _Storage()
    storage.seed('derived/v1/x/y/thumbnail/hash.jpg')
    table.upsert_entity(dict(PartitionKey='processing-artifacts-' + digest('lib'),
        RowKey='face!snap-1', payload='{}'))
    row = manifest_row('lib', 'orphaned',
        derivatives={'thumbnail': {'blob': 'derived/v1/x/y/thumbnail/hash.jpg'}},
        face_snapshots=['snap-1'])
    table.upsert_entity(row)

    result = delete_orphaned_manifest(table, storage, row)

    assert result == dict(deletedBlobs=1, deletedSnapshots=1, skippedBlobs=0, skippedSnapshots=0)
    partition = 'processing-artifacts-' + digest('lib')
    assert (partition, 'manifest!orphaned') not in table.rows
    assert (partition, 'face!snap-1') not in table.rows


def test_delete_orphaned_manifest_tolerates_already_missing_blobs_and_snapshots():
    table = FakeTable()
    storage = _Storage()  # Nothing seeded -- the referenced blob is already gone.
    row = manifest_row('lib', 'orphaned', derivatives={'thumbnail': {'blob': 'missing.jpg'}},
                       face_snapshots=['already-gone'])
    table.upsert_entity(row)
    result = delete_orphaned_manifest(table, storage, row)
    assert result == dict(deletedBlobs=0, deletedSnapshots=0, skippedBlobs=0, skippedSnapshots=0)
    partition = 'processing-artifacts-' + digest('lib')
    assert (partition, 'manifest!orphaned') not in table.rows


def test_delete_orphaned_manifest_never_deletes_a_blob_or_snapshot_shared_with_a_retained_manifest():
    """A recapture produced byte-identical derivative output (and, for this
    test, a reused snapshot id) referenced by BOTH an orphaned manifest and
    a retained one -- the shared blob/snapshot must survive."""
    table = FakeTable()
    storage = _Storage()
    storage.seed('shared.jpg')
    table.upsert_entity(dict(PartitionKey='processing-artifacts-' + digest('lib'),
        RowKey='face!shared-snap', payload='{}'))
    orphaned = manifest_row('lib', 'orphaned', derivatives={'thumbnail': {'blob': 'shared.jpg'}},
                            face_snapshots=['shared-snap'])
    retained = manifest_row('lib', 'kept', derivatives={'thumbnail': {'blob': 'shared.jpg'}},
                            face_snapshots=['shared-snap'])
    table.upsert_entity(orphaned)
    table.upsert_entity(retained)

    protected_blobs, protected_snapshots = protected_artifacts([retained])
    result = delete_orphaned_manifest(table, storage, orphaned,
        protected_blobs=protected_blobs, protected_snapshots=protected_snapshots)

    assert result == dict(deletedBlobs=0, deletedSnapshots=0, skippedBlobs=1, skippedSnapshots=1)
    partition = 'processing-artifacts-' + digest('lib')
    # The orphaned manifest row itself is still removed; the shared blob/
    # snapshot and the retained manifest survive.
    assert (partition, 'manifest!orphaned') not in table.rows
    assert (partition, 'manifest!kept') in table.rows
    assert (partition, 'face!shared-snap') in table.rows
    assert storage.existing == {'shared.jpg'}  # Blob delete_blob() never called.
