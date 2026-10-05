"""Trash membership comes from a small index, not a scan of the whole library."""
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


def test_adding_many_photos_to_an_album_checks_existence_in_batches(monkeypatch):
    from routes import albums
    table = FakeTable()
    for i in range(40):
        table.upsert_entity({'PartitionKey': 'u1', 'RowKey': f'p{i}.jpg'})
    queries = []
    real = table.query_entities
    table.query_entities = lambda f, **k: (queries.append(f), real(f, **k))[1]
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    entity = {'PartitionKey': 'u1', 'RowKey': 'al', 'filenames': '[]'}
    monkeypatch.setattr(app, '_load_album_entity', lambda uid, aid: entity)
    saved = []
    monkeypatch.setattr(app, '_save_album_entity', lambda e: saved.append(e))
    monkeypatch.setattr(app, '_album_entity_to_payload', lambda e: {})
    names = [f'p{i}.jpg' for i in range(40)] + ['ghost.jpg']
    with app.app.test_request_context('/api/albums/al/photos/add', method='POST', json={'filenames': names}):
        payload = albums.add_photos_to_album('al').get_json()
    assert len(payload['added']) == 40 and payload['errors'] == ['ghost.jpg: Not found']
    assert len(queries) == 3                                   # ceil(41 / 15), not 41 point reads
