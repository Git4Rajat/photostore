"""Tools > History: recording a run, with its outcome, and listing it back."""
import pytest

import app
from tests.fakes import FakeTable


@pytest.fixture
def http(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, 'workbench_actions_table_client', table)
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda *a, **k: None)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    client = app.app.test_client()
    client.table = table
    return client


def record(http, **overrides):
    body = {'action': 'Backfill', 'steps': ['Backfill'], 'scope': 'library', **overrides}
    return http.post('/api/tools/workbench/actions', json=body)


def stored(http):
    (row,) = http.table.rows.values()
    return row


@pytest.mark.parametrize('body', [
    {'steps': ['ocr'], 'scope': 'library'},
    {'action': 'x', 'steps': [], 'scope': 'library'},
    {'action': 'x', 'steps': ['ocr'], 'scope': 'galaxy'},
    {'action': 'x', 'steps': 'ocr', 'scope': 'library'},
])
def test_rejects_incomplete_actions(http, body):
    response = http.post('/api/tools/workbench/actions', json=body)
    assert response.status_code == 400 and response.json['code'] == 'invalid_action'
    assert http.table.rows == {}


def test_records_the_outcome_and_detail(http):
    assert record(http, outcome='partial', detail='queued 10, failed 2').json['success'] is True
    row = stored(http)
    assert (row['outcome'], row['detail']) == ('partial', 'queued 10, failed 2')


@pytest.mark.parametrize('outcome,expected', [('SUCCESS', 'success'), (' failed ', 'failed'), ('exploded', ''), (None, ''), (5, '')])
def test_outcome_is_normalised_or_dropped(http, outcome, expected):
    record(http, outcome=outcome)
    assert stored(http)['outcome'] == expected


def test_detail_is_trimmed_and_capped(http):
    record(http, outcome='failed', detail='  ' + 'x' * 500)
    assert stored(http)['detail'] == 'x' * 200


def test_older_clients_without_an_outcome_still_record(http):
    record(http)
    row = stored(http)
    assert row['outcome'] == '' and row['detail'] == '' and row['action'] == 'Backfill'


def test_history_lists_newest_first_with_outcomes_and_null_for_unknown(http):
    http.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'a', 'action': 'old', 'steps': '["x"]', 'scope': 'library',
                              'filenameCount': 0, 'force': True, 'createdAt': '2026-01-01T00:00:00+00:00'})
    http.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'b', 'action': 'new', 'steps': '["x"]', 'scope': 'library',
                              'filenameCount': 0, 'force': True, 'outcome': 'failed', 'detail': 'boom',
                              'createdAt': '2026-02-01T00:00:00+00:00'})
    actions = http.get('/api/tools/workbench/actions').json['actions']
    assert [a['action'] for a in actions] == ['new', 'old']
    assert (actions[0]['outcome'], actions[0]['detail']) == ('failed', 'boom')
    assert (actions[1]['outcome'], actions[1]['detail']) == (None, None)


def test_history_only_lists_the_callers_actions(http):
    http.table.upsert_entity({'PartitionKey': 'someone-else', 'RowKey': 'a', 'action': 'theirs', 'steps': '[]', 'scope': 'library',
                              'createdAt': '2026-01-01T00:00:00+00:00'})
    assert http.get('/api/tools/workbench/actions').json['actions'] == []
