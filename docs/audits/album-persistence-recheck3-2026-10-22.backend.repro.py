import app
from tests.fakes import FakeTable


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
