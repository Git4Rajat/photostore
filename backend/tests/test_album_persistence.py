from types import SimpleNamespace
import pytest
import app
from tests.fakes import FakeTable


def test_album_mutation_during_legacy_build_has_followup_work(monkeypatch):
    jobs, albums, dirty = FakeTable(), FakeTable(), FakeTable()
    sent = []
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', '')
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'albums_table_client', albums)
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', dirty)
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(send_message=lambda *a, **k: sent.append(a)))
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda *a: '')
    app.mark_albums_dirty('u1', ['a1'])
    def publish_older_snapshot(*a, **k):
        # The job has captured its dirty-row snapshot. This edit arrives
        # after the index input was read but before that build completes.
        app._save_album_entity({'PartitionKey':'u1','RowKey':'a1','name':'Newer'})
        return []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', publish_older_snapshot)
    app._run_index_build_job('u1', 'index-build-u1-albums', 'albums')
    assert app.get_dirty_albums('u1') == {'a1'}
    assert sent, 'Newer dirty album work needs a follow-up wake after the active build'



def test_transient_source_read_must_not_look_like_deletion_to_ui(monkeypatch):
    from routes import albums as album_routes
    from azure.core.exceptions import ServiceResponseError
    table = FakeTable()
    table.upsert_entity({'PartitionKey':'u1','RowKey':'a1','name':'Still exists','filenames':'[]'})
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1',None))
    def unavailable(*a, **k):
        raise ServiceResponseError('temporary storage outage')
    monkeypatch.setattr(table, 'get_entity', unavailable)
    with app.app.test_request_context('/albums/a1'):
        response, status = album_routes.get_album('a1')
    assert ('u1','a1') in table.rows
    assert status == 503, 'A transient source outage must not create a persistent deletion tombstone in the UI'


def test_missing_and_deleted_source_rows_still_return_404(monkeypatch):
    from routes import albums as album_routes
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'deleted', 'deleted': True})
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    for album_id in ('missing', 'deleted'):
        with app.app.test_request_context('/albums/' + album_id):
            response, status = album_routes.get_album(album_id)
        assert status == 404


def test_album_payload_exposes_source_row_revision():
    with app.app.test_request_context('/albums/a1'):
        payload = app._album_entity_to_payload({'RowKey': 'a1', 'updatedAt': '2026-10-22T10:00:00.000001+00:00'})
    assert payload['updatedAt'] == '2026-10-22T10:00:00.000001+00:00'


@pytest.mark.parametrize('rejected_status', [400, 403, 422])
def test_rejected_album_request_is_terminal_not_temporarily_unavailable(monkeypatch, rejected_status):
    from routes import albums as album_routes
    from azure.core.exceptions import HttpResponseError
    table = FakeTable()
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    def rejected(*a, **k):
        raise HttpResponseError('invalid row key', response=SimpleNamespace(status_code=rejected_status, reason='Rejected', headers={}))
    monkeypatch.setattr(table, 'get_entity', rejected)
    with app.app.test_request_context('/albums/invalid'):
        response, status = album_routes.get_album('invalid')
    assert status == rejected_status


def test_unavailable_album_enqueue_is_logged_and_retains_dirty_work(monkeypatch, caplog):
    import logging
    dirty = FakeTable()
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', dirty)
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda *a: '')
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **k: 'unavailable')
    with caplog.at_level(logging.WARNING):
        app._mark_album_dirty({'PartitionKey': 'u1', 'RowKey': 'a1'})
    assert app.get_dirty_albums('u1') == {'a1'}
    assert 'enqueue unavailable' in caplog.text


def test_batch_delete_distinguishes_lookup_outages_from_missing_rows(monkeypatch, caplog):
    from routes import albums as album_routes
    from azure.core.exceptions import ServiceResponseError
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'good', 'name': 'Good'})
    real_get = table.get_entity
    def read(partition_key, row_key):
        if row_key == 'outage':
            raise ServiceResponseError('temporary storage outage')
        return real_get(partition_key, row_key)
    monkeypatch.setattr(table, 'get_entity', read)
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_feature_available', lambda: True)
    monkeypatch.setattr(app, '_save_album_entity', table.upsert_entity)
    with app.app.test_request_context('/albums/delete-multiple', method='POST', json={'albumIds': ['outage', 'good', 'missing']}):
        payload = album_routes.delete_multiple_albums_people().get_json()
    assert payload['deletedAlbums'] == ['good']
    assert {error['albumId']: error['error'] for error in payload['albumErrors']} == {'outage': 'lookup failed', 'missing': 'Not found'}
    assert 'Album delete lookup failed' in caplog.text


@pytest.mark.parametrize('handler_name,request_json', [
    ('rename_album', {'name': 'New name'}),
    ('share_album', {'enabled': True}),
    ('revoke_album_share', None),
])
def test_mutations_reject_a_soft_deleted_album(monkeypatch, handler_name, request_json):
    # A stale cross-tab/device view can still show an album another session
    # already soft-deleted; rename/share/revoke must not succeed against it,
    # matching get_album/delete_multiple_albums_people's existing 'deleted'
    # check (see docs/audits/album-persistence-recheck3-2026-10-22.md).
    from routes import albums as album_routes
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'a1', 'name': 'Old', 'deleted': True})
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    handler = getattr(album_routes, handler_name)
    with app.app.test_request_context('/albums/a1/x', method='POST', json=request_json):
        response, status = handler('a1')
    assert status == 404
    assert table.rows[('u1', 'a1')]['name'] == 'Old', 'A rejected mutation must not alter the soft-deleted row'
