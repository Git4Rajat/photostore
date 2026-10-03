"""Upload bookkeeping/owner claims are NOT proof of an empty face namespace.

These are safety guards, not tests of an implemented fresh-upload fast path.
The existing upload protocol reuses logical filenames even when physical blob
IDs change. Keep the fallback until an immutable asset allocation exists.
"""
from __future__ import annotations

import json
import re

import pytest

import storage_utils
from test_face_by_filename_lookup import (
    AzureFaceTable, FilenameLookupTable, _complete_row, _face,
)


class UploadMetadataTable(AzureFaceTable):
    def query_entities(self, filter_str, select=None):
        match = re.fullmatch(r"PartitionKey eq '([^']*)' and \((.*)\)", filter_str)
        if match:
            self.queries.append(filter_str)
            user, clauses = match.groups()
            names = set(re.findall(r"RowKey eq '([^']*)'", clauses))
            return [dict(row) for (pk, name), row in self.rows.items()
                    if pk == user and name in names]
        return super().query_entities(filter_str, select)


@pytest.fixture
def boundary_ctx(monkeypatch):
    metadata, owners = UploadMetadataTable(), FilenameLookupTable()
    faces, lookup = AzureFaceTable(), FilenameLookupTable()
    for key, value in {
        'metadata_table_client': metadata,
        'filename_owners_table_client': owners,
        'face_table_client': faces,
        'face_by_filename_table_client': lookup,
        'blob_service_client': object(),
        'hash_index_table_client': AzureFaceTable(),
        'image_names_table_client': None,
        'face_embeddings_table_client': None,
        'person_members_table_client': None,
        'person_table_client': None,
        'face_summary_lookup': None,
        'face_summary_cache_writer': None,
    }.items():
        monkeypatch.setitem(storage_utils._CTX, key, value)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **kw: None)
    return metadata, owners, faces, lookup


def _init_tracking(kind):
    if kind == 'ranges':
        storage_utils.reset_received_ranges('u1', 'photo.jpg', 100, 'hash')
    elif kind == 'direct':
        storage_utils.reset_upload_tracking_and_reserve_blob('u1', 'photo.jpg', 100, 'hash')
    else:
        storage_utils.reset_upload_tracking_and_reserve_blobs_batch('u1', [{
            'filename': 'photo.jpg', 'total_size': 100,
            'expected_hash': 'hash', 'is_fresh': True,
        }])


@pytest.mark.parametrize('kind', ['ranges', 'direct', 'batch'])
@pytest.mark.parametrize('state', ['missing', 'dirty', 'complete', 'writing'])
def test_recent_upload_init_preserves_reused_names_and_face_generations(boundary_ctx, kind, state):
    metadata, _, faces, lookup = boundary_ctx
    rejected = {
        'PartitionKey': 'u1', 'RowKey': 'curated', 'filename': 'photo.jpg',
        'bbox': json.dumps(_face(0)['bbox']), 'rejected': True,
        'reviewStatus': 'rejected', 'personId': 'named-person',
    }
    faces.upsert_entity(rejected)
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
                            'fileHash': 'old-hash', 'anonymousImageId': 'old-blob'})
    if state == 'writing':
        storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    elif state != 'missing':
        lookup.upsert_entity(_complete_row(['curated'], state=state))
    before = {key: dict(row) for key, row in lookup.rows.items()}
    _init_tracking(kind)
    assert metadata.get_entity('u1', 'photo.jpg')['upload_started_at']
    assert metadata.get_entity('u1', 'photo.jpg')['fileHash'] == 'old-hash'
    assert metadata.get_entity('u1', 'photo.jpg')['anonymousImageId'] == 'old-blob'
    assert lookup.rows == before
    assert faces.rows[('u1', 'curated')] == rejected
    faces.queries.clear()
    faces.writes.clear()
    if state == 'writing':
        with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
            storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
        assert lookup.rows == before
        assert faces.queries == []
    else:
        assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)]) == []
        assert len(faces.queries) == (0 if state == 'complete' else 1)
        assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == ['curated']
    assert faces.writes == []
    assert faces.rows[('u1', 'curated')] == rejected


def test_new_metadata_and_successful_owner_create_do_not_exclude_orphan_faces(boundary_ctx):
    metadata, _, faces, _ = boundary_ctx
    orphan = {'PartitionKey': 'u1', 'RowKey': 'orphan', 'filename': 'photo.jpg',
              'bbox': json.dumps(_face(0)['bbox']), 'reviewStatus': 'rejected'}
    faces.upsert_entity(orphan)
    # Owner/metadata creation does not prove index coverage for legacy faces.
    assert storage_utils._claim_filename_owner('u1', 'photo.jpg', 'hash')
    assert metadata.rows == {}
    metadata.upsert_entity(storage_utils.get_or_create_metadata('u1', 'photo.jpg'))
    _init_tracking('direct')
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)]) == []
    assert len(faces.queries) == 1
    assert faces.rows[('u1', 'orphan')] == orphan


@pytest.mark.parametrize('state', ['missing', 'dirty', 'complete', 'writing'])
def test_duplicate_finalize_never_initializes_or_overwrites_face_lookup(boundary_ctx, state):
    metadata, _, faces, lookup = boundary_ctx
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'fileHash': 'hash'})
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'existing', 'filename': 'photo.jpg',
                         'rejected': True})
    if state == 'writing':
        storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    elif state != 'missing':
        lookup.upsert_entity(_complete_row(['existing'], state=state))
    before = {key: dict(row) for key, row in lookup.rows.items()}
    for _ in range(2):
        _, name = storage_utils.finalize_uploaded_file(
            'u1', 'photo.jpg', 'image/jpeg', client_sha256='hash',
            anonymous_blob_name='new-physical-blob',
        )
        assert name == 'photo.jpg'
        assert lookup.rows == before
    assert faces.queries == []
    assert faces.rows[('u1', 'existing')]['rejected'] is True


def test_released_owner_can_be_recreated_while_faces_still_exist(boundary_ctx):
    """Historical incomplete cleanup must remain discoverable on reuse."""
    metadata, _, faces, _ = boundary_ctx
    assert storage_utils._claim_filename_owner('u1', 'photo.jpg', 'old-hash')
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'fileHash': 'old-hash'})
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'old-face', 'filename': 'photo.jpg',
                         'bbox': json.dumps(_face(0)['bbox']), 'rejected': True})
    metadata.delete_entity(partition_key='u1', row_key='photo.jpg')
    storage_utils.delete_filename_owner_entry('u1', 'photo.jpg')
    assert storage_utils._claim_filename_owner('u1', 'photo.jpg', 'new-hash')
    _init_tracking('direct')
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)]) == []
    assert len(faces.queries) == 1
    assert faces.rows[('u1', 'old-face')]['rejected'] is True


def test_complete_zero_is_query_free_but_missing_new_upload_is_not(boundary_ctx):
    """Only authoritative reconciliation proves zero, not index absence."""
    _, _, faces, lookup = boundary_ctx
    _init_tracking('direct')
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
    storage_utils._store_client_face_entities('u1', 'photo.jpg', [])
    assert len(faces.queries) == 1
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []
    faces.queries.clear()
    storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert faces.queries == []
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'complete'


def test_missing_lookup_preserves_rejection_at_identical_deterministic_id(boundary_ctx):
    _, _, faces, _ = boundary_ctx
    detection = _face(0)
    face_id = storage_utils._deterministic_face_id('u1', 'photo.jpg', detection)
    rejected = {'PartitionKey': 'u1', 'RowKey': face_id, 'filename': 'photo.jpg',
                'bbox': json.dumps(detection['bbox']), 'rejected': True,
                'personId': 'curated', 'confirmedByUser': True}
    faces.upsert_entity(rejected)
    faces.writes.clear()
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [detection]) == []
    assert faces.rows[('u1', face_id)] == rejected
    assert faces.writes == []
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == [face_id]