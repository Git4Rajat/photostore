import json

import pytest

import app


@pytest.mark.parametrize('scope,label', [
    ('full', 'Full library indexes'), ('light', 'Photo browsing index'),
    ('people', 'People index'), ('dedup', 'Duplicate-check index'),
    ('dedup-idle', 'Duplicate-check index'),
])
@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'failed'])
def test_index_notification_identifies_scope(scope, label, status):
    row = {'jobId': 'job', 'jobType': app.INDEX_BUILD_JOB_TYPE, 'indexScope': scope,
           'status': status, 'error': 'Worker restarted' if status == 'failed' else ''}
    result = app._humanize_job(row)
    assert result['kind'] == 'index_build'
    assert result['indexScope'] == scope
    assert result['title'].startswith(label)
    if status == 'done':
        assert result['message'] and 'Your library index finished' not in result['message']
    elif status == 'failed':
        assert result['title'].endswith('update failed')
        assert result['message'] == 'Worker restarted'


@pytest.mark.parametrize('scope', ['full', 'light', 'people', 'dedup', 'dedup-idle'])
def test_legacy_index_rows_are_classified_by_complete_job_id(scope):
    user = 'owner-dedup-idle'
    job_id = app._index_build_job_id(user) + (f'-{scope}' if scope != 'full' else '')
    for identity in [{'userId': user}, {'PartitionKey': user}]:
        result = app._humanize_job({'jobId': job_id, 'jobType': app.INDEX_BUILD_JOB_TYPE,
                                   'status': 'done', **identity})
        assert result['indexScope'] == scope


def test_result_scope_and_explicit_scope_are_retained_for_nonstandard_job_ids():
    row = {'jobId': 'legacy-job', 'jobType': app.INDEX_BUILD_JOB_TYPE, 'status': 'done',
           'result': json.dumps({'scope': 'people'})}
    assert app._humanize_job(row)['indexScope'] == 'people'
    assert app._humanize_job({**row, 'indexScope': 'light'})['indexScope'] == 'light'


@pytest.mark.parametrize('scope', ['full', 'light', 'people', 'dedup', 'dedup-idle'])
def test_enqueue_persists_scope_without_needing_a_delayed_result(monkeypatch, scope):
    class Queue:
        def send_message(self, *args, **kwargs):
            pass

    rows = []
    from tests.test_index_priming import _dedicated_queue
    _dedicated_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, '_index_build_job_active', lambda _: False)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args, **fields: rows.append(fields))
    assert app.enqueue_index_build('owner', scope=scope) == 'queued'
    assert rows[0]['indexScope'] == scope


def test_worker_persists_scope_before_work_or_failure(monkeypatch):
    rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args, **fields: rows.append((args, fields)))
    def fail(*args, **kwargs):
        raise RuntimeError('test worker failure')
    monkeypatch.setattr(app, 'get_dirty_people', fail)
    with pytest.raises(RuntimeError, match='test worker failure'):
        app._run_index_build_job('owner', 'custom-job', scope='people')
    assert rows[0][1]['indexScope'] == 'people'
    assert rows[-1][0][3] == 'failed'


@pytest.mark.parametrize('status', ['deferred', 'no_base'])
def test_search_waiting_for_repair_does_not_claim_to_be_updated(status):
    result = app._humanize_job({'jobId': 'job', 'jobType': app.INDEX_BUILD_JOB_TYPE,
                               'indexScope': 'search', 'status': 'done', 'result': {'status': status}})
    assert result['title'] == 'Search index maintenance scheduled'
    assert 'not yet current' in result['message']
