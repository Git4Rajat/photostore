"""Trash membership comes from a small index, not a scan of the whole library."""
import json

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

import app
from routes import photos
from tests.fakes import FakeTable


class _Meta(FakeTable):
    """Metadata fake that counts scans (query_entities) versus point reads."""

    def __init__(self):
        super().__init__()
        self.scans = 0

    def query_entities(self, filter_str, select=None, **kw):
        if "processing_state eq 'deleted'" in filter_str:
            self.scans += 1
        return [dict(v) for (p, _), v in self.rows.items() if "processing_state eq 'deleted'" not in filter_str or v.get('processing_state') == 'deleted']


def _setup(monkeypatch, deleted=('a.jpg', 'b.jpg')):
    meta, trash = _Meta(), FakeTable()
    for i, name in enumerate(['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg']):
        row = {'PartitionKey': 'u1', 'RowKey': name, 'processing_state': 'deleted' if name in deleted else 'active',
               'deletedAt': f'2026-01-0{i + 1}T00:00:00+00:00' if name in deleted else ''}
        meta.upsert_entity(row)
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(app, 'trash_index_table_client', trash)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: (lambda r: dict(r) if r else None)(meta.rows.get((uid, name))))
    return meta, trash


def test_first_call_backfills_with_one_scan_then_never_scans_again(monkeypatch):
    meta, trash = _setup(monkeypatch)
    entries = app._trash_index_entries('u1')
    assert [e['RowKey'] for e in entries] == ['b.jpg', 'a.jpg']            # newest first
    assert meta.scans == 1 and ('u1', '__init__') in trash.rows
    app._trash_index_entries('u1')
    app._trash_index_entries('u1')
    assert meta.scans == 1                                                   # served from the index


def test_soft_delete_and_restore_keep_the_index_current(monkeypatch):
    meta, trash = _setup(monkeypatch, deleted=())
    app._trash_index_entries('u1')                                           # initialise (empty)
    monkeypatch.setattr(app, 'BROWSER_PROCESSING_STATUS_FIELDS', [], raising=False)
    app._mark_processing_deleted_for_file('u1', 'c.jpg')
    assert [e['RowKey'] for e in app._trash_index_entries('u1')] == ['c.jpg']
    app._restore_deleted_file('u1', 'c.jpg')
    assert app._trash_index_entries('u1') == []
    assert meta.scans == 1


def test_listing_prunes_entries_that_were_purged_or_restored(monkeypatch):
    meta, trash = _setup(monkeypatch)
    app._trash_index_entries('u1')
    meta.rows.pop(('u1', 'a.jpg'))                                           # purged behind the index's back
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_build_photo_summaries_page', lambda uid, items, names: [{'filename': n} for n, _ in items])
    with app.app.test_request_context('/api/photos/trash'):
        payload = photos.list_trashed_photos().get_json()
    assert [p['filename'] for p in payload['photos']] == ['b.jpg']
    assert payload['total'] == 1
    assert ('u1', 'a.jpg') not in trash.rows                                 # pruned
    assert meta.scans == 1


def test_without_an_index_table_the_scan_is_used(monkeypatch):
    meta, _ = _setup(monkeypatch)
    monkeypatch.setattr(app, 'trash_index_table_client', None)
    assert len(app._trash_index_entries('u1')) == 2 and meta.scans == 1


def test_delete_replay_repairs_failed_trash_write_without_resetting_restore_snapshot(monkeypatch):
    meta, trash = _setup(monkeypatch, deleted=())
    meta.rows[('u1', 'c.jpg')]['thumbnail_status'] = 'done'
    monkeypatch.setattr(app, 'BROWSER_PROCESSING_STATUS_FIELDS', [('thumbnail_status', 'thumbnail')])
    app._trash_index_entries('u1')
    original = trash.upsert_entity
    failed = []
    def unavailable_once(entity, **kwargs):
        if not failed:
            failed.append(1)
            raise RuntimeError('trash unavailable')
        return original(entity, **kwargs)
    monkeypatch.setattr(trash, 'upsert_entity', unavailable_once)
    with pytest.raises(RuntimeError, match='trash unavailable'):
        app._mark_processing_deleted_for_file('u1', 'c.jpg')
    committed = dict(meta.rows[('u1', 'c.jpg')])
    version = meta.versions[('u1', 'c.jpg')]
    assert committed['processing_state'] == 'deleted'
    assert ('u1', 'c.jpg') not in trash.rows

    replay = app._mark_processing_deleted_for_file('u1', 'c.jpg')
    assert replay['deletedAt'] == committed['deletedAt']
    assert replay['preDeleteStatuses'] == committed['preDeleteStatuses']
    assert meta.versions[('u1', 'c.jpg')] == version
    assert [e['RowKey'] for e in app._trash_index_entries('u1')] == ['c.jpg']
    assert meta.scans == 1
    assert app._restore_deleted_file('u1', 'c.jpg')['thumbnail_status'] == 'done'


@pytest.mark.parametrize('restoring', [False, True])
def test_delete_and_restore_retry_concurrent_metadata_edits(monkeypatch, restoring):
    meta, _ = _setup(monkeypatch, deleted=('c.jpg',) if restoring else ())
    row = meta.rows[('u1', 'c.jpg')]
    row.update(rating=1, thumbnail_status='deleted' if restoring else 'pending',
        preDeleteStatuses=json.dumps({'thumbnail_status': 'done'}))
    monkeypatch.setattr(app, 'BROWSER_PROCESSING_STATUS_FIELDS', [('thumbnail_status', 'thumbnail')])
    original = meta.update_entity
    attempts = []
    def racing_update(entity, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            current = dict(meta.rows[('u1', 'c.jpg')], rating=5)
            if not restoring:
                current['thumbnail_status'] = 'done'
            meta.upsert_entity(current)
        return original(entity, **kwargs)
    monkeypatch.setattr(meta, 'update_entity', racing_update)
    action = app._restore_deleted_file if restoring else app._mark_processing_deleted_for_file
    saved = action('u1', 'c.jpg')
    assert len(attempts) == 2
    assert all(call['etag'] and call['match_condition'] for call in attempts)
    assert meta.rows[('u1', 'c.jpg')]['rating'] == 5
    if restoring:
        assert saved['thumbnail_status'] == 'done'
    else:
        assert json.loads(saved['preDeleteStatuses'])['thumbnail_status'] == 'done'


@pytest.mark.parametrize('restoring', [False, True])
def test_delete_and_restore_do_not_recreate_a_concurrently_purged_source(monkeypatch, restoring):
    meta, trash = _setup(monkeypatch, deleted=('c.jpg',) if restoring else ())
    def purged(entity, **kwargs):
        meta.rows.pop(('u1', 'c.jpg'))
        raise ResourceNotFoundError('purged')
    monkeypatch.setattr(meta, 'update_entity', purged)
    action = app._restore_deleted_file if restoring else app._mark_processing_deleted_for_file
    assert action('u1', 'c.jpg') is None
    assert ('u1', 'c.jpg') not in meta.rows
    assert ('u1', 'c.jpg') not in trash.rows


def test_delete_propagates_lookup_outage_and_bounded_contention(monkeypatch):
    meta, _ = _setup(monkeypatch, deleted=())
    def contention(*args, **kwargs):
        raise ResourceModifiedError('changed')
    monkeypatch.setattr(meta, 'update_entity', contention)
    with pytest.raises(RuntimeError, match='Concurrent photo deletion'):
        app._mark_processing_deleted_for_file('u1', 'c.jpg')
    assert meta.rows[('u1', 'c.jpg')]['processing_state'] == 'active'
    def unavailable(*args, **kwargs):
        raise RuntimeError('metadata unavailable')
    monkeypatch.setattr(meta, 'get_entity', unavailable)
    with pytest.raises(RuntimeError, match='metadata unavailable'):
        app._mark_processing_deleted_for_file('u1', 'c.jpg')


def test_page_rows_are_fetched_in_batches_of_15_not_one_by_one(monkeypatch):
    table = FakeTable()
    names = [f'p{i}.jpg' for i in range(48)]
    for n in names:
        table.upsert_entity({'PartitionKey': 'u1', 'RowKey': n, 'rating': 1})
    queries, points = [], []
    real_query, real_get = table.query_entities, table.get_entity
    table.query_entities = lambda f, **k: (queries.append(f), real_query(f, **k))[1]
    table.get_entity = lambda partition_key, row_key: (points.append(row_key), real_get(partition_key, row_key))[1]
    monkeypatch.setattr(app, 'metadata_table_client', table)
    out = app._get_metadata_entities('u1', names + ['missing.jpg'])
    assert len(queries) == 4 and not points                      # ceil(49 / 15) round trips
    assert out['p7.jpg']['rating'] == 1 and out['missing.jpg'] is None


def test_a_failing_batch_query_falls_back_to_point_reads(monkeypatch):
    table = FakeTable()
    for n in ('a.jpg', 'b.jpg'):
        table.upsert_entity({'PartitionKey': 'u1', 'RowKey': n, 'rating': 2})
    table.query_entities = lambda f, **k: (_ for _ in ()).throw(RuntimeError('too many comparisons'))
    monkeypatch.setattr(app, 'metadata_table_client', table)
    assert app._get_metadata_entities('u1', ['a.jpg', 'b.jpg'])['b.jpg']['rating'] == 2


def test_adding_many_photos_to_an_album_checks_existence_in_the_library_database(monkeypatch):
    from routes import albums
    table = FakeTable()
    for i in range(41):                                        # p40 was uploaded seconds ago: only in the table
        table.upsert_entity({'PartitionKey': 'u1', 'RowKey': f'p{i}.jpg'})
    reads = []
    real_get, real_query = table.get_entity, table.query_entities
    table.get_entity = lambda partition_key, row_key: (reads.append(row_key), real_get(partition_key, row_key))[1]
    table.query_entities = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no OR-filter queries'))
    monkeypatch.setattr(app, 'metadata_table_client', table)

    class FakeDb:
        def existing_filenames(self, names):
            return {n for n in names if n.startswith('p') and n != 'p40.jpg'}
    monkeypatch.setattr(app, '_open_library_db', lambda uid: FakeDb())
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    entity = {'PartitionKey': 'u1', 'RowKey': 'al', 'filenames': '[]'}
    monkeypatch.setattr(app, '_load_album_entity', lambda uid, aid: entity)
    monkeypatch.setattr(app, '_save_album_entity', lambda e: None)
    monkeypatch.setattr(app, '_album_entity_to_payload', lambda e: {})
    names = [f'p{i}.jpg' for i in range(41)] + ['ghost.jpg']
    with app.app.test_request_context('/api/albums/al/photos/add', method='POST', json={'filenames': names}):
        payload = albums.add_photos_to_album('al').get_json()
    assert len(payload['added']) == 41 and payload['errors'] == ['ghost.jpg: Not found']
    assert sorted(reads) == ['ghost.jpg', 'p40.jpg']           # only what the database did not know
