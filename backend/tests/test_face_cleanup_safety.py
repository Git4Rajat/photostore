"""Cleanup must not publish false-empty indexes or release names on failure."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import ResourceNotFoundError

import app
import storage_utils
from test_face_by_filename_lookup import AzureFaceTable, FilenameLookupTable, _face


@pytest.fixture
def cleanup_ctx(monkeypatch):
    faces, lookup = AzureFaceTable(), FilenameLookupTable()
    people, metadata, owners = AzureFaceTable(), AzureFaceTable(), FilenameLookupTable()
    for name, table in [('face_table_client', faces), ('person_table_client', people),
                        ('metadata_table_client', metadata)]:
        monkeypatch.setattr(app, name, table)
        monkeypatch.setitem(storage_utils._CTX, name, table)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', lookup)
    monkeypatch.setitem(storage_utils._CTX, 'filename_owners_table_client', owners)
    for key in ('face_embeddings_table_client', 'person_members_table_client',
                'face_summary_lookup', 'face_summary_cache_writer'):
        monkeypatch.setitem(storage_utils._CTX, key, None)
    monkeypatch.setattr(app, '_create_people_repair_snapshot', lambda *a, **kw: 'snapshot')
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **kw: {})
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filename', lambda *a, **kw: {})
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_shared_names_in_batch', lambda *a: set())
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda *a: [])
    for name in ('delete_hash_index_entry', 'delete_embeddings_entry',
                 '_batch_remove_job_rows', '_batch_remove_filenames_from_albums',
                 '_invalidate_metadata_scan_cache', 'touch_user_search_indexes_state'):
        monkeypatch.setattr(app, name, lambda *a, **kw: None)
    return faces, lookup, people, metadata, owners


def _seed_face(faces, face_id='old', filename='photo.jpg', **overrides):
    row = {'PartitionKey': 'u1', 'RowKey': face_id, 'filename': filename,
           **_face(0), **overrides}
    row['bbox'] = json.dumps(row['bbox'])
    faces.upsert_entity(row)
    return row


def _outage(*a, **kw):
    raise OSError('storage outage')


@pytest.mark.parametrize('single', [False, True])
@pytest.mark.parametrize('failure', ['query', 'page', 'delete', 'embedding', 'people', 'member'])
def test_cleanup_failure_keeps_lookup_unknown(cleanup_ctx, monkeypatch, single, failure):
    faces, lookup, people, _, _ = cleanup_ctx
    old = _seed_face(faces, personId='p')
    people.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p', 'faceIds': '["old"]'})
    if failure == 'query':
        monkeypatch.setattr(faces, 'query_entities', _outage)
    elif failure == 'page':
        def pages(*a, **kw):
            yield dict(old)
            _outage()
        monkeypatch.setattr(faces, 'query_entities', pages)
    elif failure == 'delete':
        monkeypatch.setattr(faces, 'delete_entity', _outage)
    elif failure == 'embedding':
        monkeypatch.setattr(app, 'delete_face_embeddings_entry', _outage)
    elif failure == 'people':
        monkeypatch.setattr(people, 'query_entities', _outage)
    else:
        monkeypatch.setattr(app, '_remove_face_person_member', _outage)
    with pytest.raises(OSError, match='storage outage'):
        if single:
            app._remove_faces_for_filename('u1', 'photo.jpg')
        else:
            app._batch_remove_faces_for_filenames('u1', {'photo.jpg'})
    assert ('u1', 'old') in faces.rows
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'


def test_cleanup_holds_generation_before_scan_and_delete(cleanup_ctx, monkeypatch):
    faces, lookup, _, _, _ = cleanup_ctx
    _seed_face(faces)
    query, delete = faces.query_entities, faces.delete_entity
    blocked = []

    def assert_writer_blocked():
        assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'writing'
        with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
            storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(20)])
        blocked.append(True)

    def query_under_lease(*a, **kw):
        assert_writer_blocked()
        return query(*a, **kw)

    def delete_under_lease(*a, **kw):
        assert_writer_blocked()
        return delete(*a, **kw)

    monkeypatch.setattr(faces, 'query_entities', query_under_lease)
    monkeypatch.setattr(faces, 'delete_entity', delete_under_lease)
    assert app._batch_remove_faces_for_filenames('u1', {'photo.jpg'}) == set()
    assert len(blocked) == 2
    assert faces.rows == {}
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


def test_expired_cleanup_lease_aborts_before_deletion(cleanup_ctx, monkeypatch):
    faces, lookup, _, _, _ = cleanup_ctx
    _seed_face(faces)
    query = faces.query_entities

    def expire_during_scan(*a, **kw):
        rows = query(*a, **kw)
        row = dict(lookup.rows[('u1', 'photo.jpg')])
        row['leaseExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        lookup.upsert_entity(row)
        return rows

    monkeypatch.setattr(faces, 'query_entities', expire_during_scan)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='lease'):
        app._batch_remove_faces_for_filenames('u1', {'photo.jpg'})
    assert ('u1', 'old') in faces.rows
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_active_writer_blocks_cleanup_without_scan(cleanup_ctx):
    faces, lookup, _, _, _ = cleanup_ctx
    _seed_face(faces)
    storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
        app._batch_remove_faces_for_filenames('u1', {'photo.jpg'})
    assert faces.queries == []
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'writing'


def test_cleanup_requires_coordination_table(cleanup_ctx, monkeypatch):
    faces, _, _, _, _ = cleanup_ctx
    _seed_face(faces)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', None)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='unavailable'):
        app._batch_remove_faces_for_filenames('u1', {'photo.jpg'})
    assert faces.queries == []
    assert ('u1', 'old') in faces.rows


def test_partial_acquisition_releases_earlier_generations_as_dirty(cleanup_ctx):
    faces, lookup, _, _, _ = cleanup_ctx
    storage_utils._begin_face_filename_write('u1', 'b.jpg')
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
        app._batch_remove_faces_for_filenames('u1', {'a.jpg', 'b.jpg'})
    assert faces.queries == []
    assert lookup.rows[('u1', 'a.jpg')]['state'] == 'dirty'
    assert lookup.rows[('u1', 'b.jpg')]['state'] == 'writing'


def test_partial_publication_dirties_all_acquired_filenames(cleanup_ctx, monkeypatch):
    _, lookup, _, _, _ = cleanup_ctx
    finish = app._finish_face_filename_write

    def fail_second(user, filename, generation, ids):
        if filename == 'b.jpg':
            _outage()
        finish(user, filename, generation, ids)

    monkeypatch.setattr(app, '_finish_face_filename_write', fail_second)
    with pytest.raises(OSError):
        app._batch_remove_faces_for_filenames('u1', {'a.jpg', 'b.jpg'})
    assert all(row['state'] == 'dirty' for row in lookup.rows.values())


@pytest.mark.parametrize('failure', ['query', 'delete', 'metadata'])
def test_hard_delete_failure_retains_retry_anchor_and_owner(cleanup_ctx, monkeypatch, failure):
    faces, lookup, _, metadata, owners = cleanup_ctx
    _seed_face(faces)
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
                            'processing_state': 'deleted', 'fileHash': 'old-hash'})
    owners.upsert_entity({'PartitionKey': 'photo.jpg', 'RowKey': 'u1', 'fileHash': 'old-hash'})
    target = metadata if failure == 'metadata' else faces
    attr = 'query_entities' if failure == 'query' else 'delete_entity'
    original = getattr(target, attr)
    monkeypatch.setattr(target, attr, _outage)
    deleted, errors = app._hard_delete_photos_now('u1', ['photo.jpg'])
    assert deleted == [] and len(errors) == 1
    assert ('u1', 'photo.jpg') in metadata.rows
    assert ('photo.jpg', 'u1') in owners.rows
    if failure != 'metadata':
        assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    monkeypatch.setattr(target, attr, original)
    assert app._hard_delete_photos_now('u1', ['photo.jpg']) == (['photo.jpg'], [])
    assert metadata.rows == {} and owners.rows == {} and faces.rows == {}
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


def test_hard_delete_releases_owner_only_after_complete_zero(cleanup_ctx, monkeypatch):
    faces, _, _, metadata, owners = cleanup_ctx
    _seed_face(faces)
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'processing_state': 'deleted'})
    owners.upsert_entity({'PartitionKey': 'photo.jpg', 'RowKey': 'u1'})
    release = app.delete_filename_owner_entry

    def release_after_cleanup(user, filename):
        assert faces.rows == {} and metadata.rows == {}
        assert storage_utils.get_face_ids_for_filename(user, filename) == []
        release(user, filename)

    monkeypatch.setattr(app, 'delete_filename_owner_entry', release_after_cleanup)
    assert app._hard_delete_photos_now('u1', ['photo.jpg']) == (['photo.jpg'], [])
    assert owners.rows == {}


def test_cleanup_not_found_delete_is_idempotent(cleanup_ctx, monkeypatch):
    faces, _, _, _, _ = cleanup_ctx
    _seed_face(faces)
    delete = faces.delete_entity

    def already_deleted(*a, **kw):
        delete(*a, **kw)
        raise ResourceNotFoundError('Already gone')

    monkeypatch.setattr(faces, 'delete_entity', already_deleted)
    app._batch_remove_faces_for_filenames('u1', {'photo.jpg'})
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


def test_batch_renewal_is_not_quadratic_in_files_and_people(cleanup_ctx, monkeypatch):
    faces, lookup, people, _, _ = cleanup_ctx
    monkeypatch.setattr(storage_utils.time, 'monotonic', lambda: 0)
    filenames = set()
    for index in range(20):
        filename, face_id, person_id = f'{index}.jpg', f'f{index}', f'p{index}'
        filenames.add(filename)
        _seed_face(faces, face_id, filename, personId=person_id)
        people.upsert_entity({'PartitionKey': 'u1', 'RowKey': person_id,
                             'faceIds': json.dumps([face_id])})
    app._batch_remove_faces_for_filenames('u1', filenames)
    # One forced scan-boundary renewal, three source/shadow renewals and
    # renewal+completion per photo, not all filenames renewed per person.
    assert len(lookup.updates) <= 20 * 8
    assert all(storage_utils.get_face_ids_for_filename('u1', name) == [] for name in filenames)


def _seed_duplicates(faces):
    _seed_face(faces, 'first', confirmedByUser=True, personId='named')
    _seed_face(faces, 'second')
    _seed_face(faces, 'unrelated', bbox=_face(40)['bbox'])


def test_dedupe_establishes_missing_lookup_with_all_survivors(cleanup_ctx):
    faces, lookup, _, _, _ = cleanup_ctx
    _seed_duplicates(faces)
    assert lookup.rows == {}
    result = app._dedupe_duplicate_faces('u1', dry_run=False)
    assert result['duplicateGroups'] == 1
    canonical_id = result['groups'][0]['canonicalFaceId']
    assert set(storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')) == {canonical_id, 'unrelated'}
    assert faces.rows[('u1', canonical_id)]['confirmedByUser'] is True
    assert faces.rows[('u1', canonical_id)]['personId'] == 'named'


def test_active_writer_prevents_dedupe_mutations(cleanup_ctx):
    faces, _, _, _, _ = cleanup_ctx
    _seed_duplicates(faces)
    before = dict(faces.rows)
    storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
        app._dedupe_duplicate_faces('u1', dry_run=False)
    assert faces.rows == before


@pytest.mark.parametrize('failure', ['upsert', 'delete', 'scan', 'final_scan'])
def test_dedupe_failure_leaves_lookup_dirty(cleanup_ctx, monkeypatch, failure):
    faces, lookup, _, _, _ = cleanup_ctx
    _seed_duplicates(faces)
    if failure in ('upsert', 'delete'):
        monkeypatch.setattr(faces, failure + '_entity', _outage)
    else:
        query = faces.query_entities
        calls = 0

        def fail_scan(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == (2 if failure == 'scan' else 3):
                _outage()
            return query(*a, **kw)

        monkeypatch.setattr(faces, 'query_entities', fail_scan)
    with pytest.raises(OSError, match='storage outage'):
        app._dedupe_duplicate_faces('u1', dry_run=False)
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_dedupe_rescans_after_acquisition(cleanup_ctx, monkeypatch):
    faces, _, _, _, _ = cleanup_ctx
    _seed_duplicates(faces)
    query = faces.query_entities
    calls = 0

    def fresh_scan(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            # A row changed between discovery and lease acquisition. It must
            # be preserved from the fresh scan, not overwritten from discovery.
            row = dict(faces.rows[('u1', 'first')])
            row['personId'] = 'fresh-name'
            faces.upsert_entity(row)
        return query(*a, **kw)

    monkeypatch.setattr(faces, 'query_entities', fresh_scan)
    result = app._dedupe_duplicate_faces('u1', dry_run=False)
    canonical_id = result['groups'][0]['canonicalFaceId']
    assert faces.rows[('u1', canonical_id)]['personId'] == 'fresh-name'