from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import app
from routes import tools


@pytest.fixture
def tables(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library-A', None))
    rows = {
        'photo': {'rating': 5, 'likes': 2, 'likedBy': '["library-A","other"]', 'rotation': 90},
        'person': {'name': 'Bob'}, 'album': {'name': 'Trip'},
    }
    clients = {}
    for kind, attr in [('photo', 'metadata_table_client'), ('person', 'person_table_client'), ('album', 'albums_table_client')]:
        clients[kind] = SimpleNamespace(get_entity=Mock(return_value=rows[kind]))
        monkeypatch.setattr(app, attr, clients[kind])
    return clients


def reconcile(edits, **extra):
    with app.app.test_request_context('/api/tools/pending-edits/reconcile', method='POST', json={'edits': edits, **extra}):
        return tools.reconcile_pending_edits()


def test_reads_source_rows_in_authenticated_library_only(tables):
    response = reconcile([
        {'kind': 'photo', 'id': 'one.jpg', 'fields': {k: 'token' for k in ['rating', 'likes', 'liked', 'rotation']}},
        {'kind': 'person', 'id': 'p1', 'fields': {'name': 'token'}},
        {'kind': 'album', 'id': 'a1', 'fields': {'name': 'token'}},
    ], libraryId='library-B')
    assert response.headers['Cache-Control'] == 'no-store'
    data = response.get_json()
    assert data['libraryId'] == 'library-A'
    assert [row['values'] for row in data['rows']] == [
        {'rating': 5, 'likes': 2, 'liked': True, 'rotation': 90}, {'name': 'Bob'}, {'name': 'Trip'},
    ]
    for kind, entity_id in [('photo', 'one.jpg'), ('person', 'p1'), ('album', 'a1')]:
        tables[kind].get_entity.assert_called_once_with(partition_key='library-A', row_key=entity_id)


def test_source_unavailability_does_not_acknowledge_or_delete(tables):
    tables['photo'].get_entity.side_effect = RuntimeError('storage unavailable')
    assert reconcile([{'kind': 'photo', 'id': 'one.jpg', 'fields': {'rating': 't'}}]).get_json()['rows'] == []


def test_missing_source_is_distinguished_from_unavailability(tables):
    tables['person'].get_entity.side_effect = app.ResourceNotFoundError('missing')
    row = reconcile([{'kind': 'person', 'id': 'p1', 'fields': {'name': 't'}}]).get_json()['rows'][0]
    assert row['deleted'] is True


@pytest.mark.parametrize('kind,entity', [('photo', {'processing_state': 'deleted'}), ('album', {'deleted': True})])
def test_soft_deleted_sources_are_not_treated_as_live_values(tables, kind, entity):
    tables[kind].get_entity.return_value = entity
    fields = {'rating': 't'} if kind == 'photo' else {'name': 't'}
    row = reconcile([{'kind': kind, 'id': 'id', 'fields': fields}]).get_json()['rows'][0]
    assert row['deleted'] is True


@pytest.mark.parametrize('edits', [None, {}, [None], [{'kind': []}],
    [{'kind': 'photo', 'id': 'id', 'fields': {'exifData': 't'}}],
    [{'kind': 'person', 'id': 'id', 'fields': {'name': []}}],
    [{'kind': 'person', 'id': '', 'fields': {'name': 't'}}],
    [{'kind': 'album', 'id': 'id', 'fields': {}}],
    [{'kind': 'album', 'id': 'id', 'fields': {'name': 't'}}] * 51])
def test_invalid_or_unbounded_requests_are_rejected(tables, edits):
    assert reconcile(edits)[1] == 400
    assert not any(table.get_entity.called for table in tables.values())


def test_requires_authentication(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: (None, ('unauthorized', 401)))
    assert reconcile([]) == ('unauthorized', 401)


@pytest.mark.parametrize('body', [[], ['bad'], 42, 'bad'])
def test_invalid_json_root_is_rejected(tables, body):
    with app.app.test_request_context('/api/tools/pending-edits/reconcile', method='POST', json=body):
        assert tools.reconcile_pending_edits()[1] == 400
