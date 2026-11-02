import pytest

import app
import library_utils
from fakes import make_store


@pytest.fixture
def cleanup_flow(monkeypatch):
    store = make_store()
    for user in ('owner', 'bob'):
        store.create_user(email=f'{user}@example.com', user_id=user)
        store.ensure_personal_library(user)
    store.add_membership('bob', 'owner')
    monkeypatch.setattr(app, 'library_store', store)
    monkeypatch.setattr(app, 'SESSION_SECRET', 'cleanup-status-test-secret-with-32-bytes')
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    jobs, enqueued = {}, []
    def enqueue(library_id, account_id, request_id):
        job_id = f'job-{len(enqueued) + 1}'
        enqueued.append((library_id, account_id, request_id))
        store.set_cleanup_in_progress(library_id, job_id)
        jobs[(library_id, job_id)] = {'status': 'queued'}
        return {'status': 'queued', 'jobId': job_id}
    monkeypatch.setattr(app, '_enqueue_library_clean_job', enqueue)
    monkeypatch.setattr(app, '_get_job_row', lambda library_id, job_id: jobs.get((library_id, job_id)))
    monkeypatch.setattr(app, '_reconcile_stale_library_cleanup', lambda *_a, **_k: None)
    return store, jobs, enqueued


def headers(user='owner', library_id='owner'):
    return {'Authorization': f'Bearer {app._issue_session_for(user, library_id=library_id)}'}


def status(client, token, **kwargs):
    return client.get('/api/library/clean/confirmation-status', query_string={'token': token}, headers=kwargs.get('headers', headers()))


def test_reload_reads_same_job_without_reconfirming_or_restarting(cleanup_flow):
    store, jobs, enqueued = cleanup_flow
    request_id, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'])
    client = app.app.test_client()
    response = status(client, tokens['owner'])
    assert response.get_json() == {'status': 'pending'}
    assert response.headers['Cache-Control'] == 'no-store'
    assert enqueued == []
    assert store.get_active_clean_request('owner')['confirmedUserIds'] == ''
    response = client.post('/api/library/clean/confirm', json={'token': tokens['owner']}, headers=headers())
    assert response.status_code == 200
    assert response.get_json()['jobId'] == 'job-1'
    # A new browser/page request has no component or localStorage state.
    reloaded = app.app.test_client()
    assert status(reloaded, tokens['owner']).get_json()['status'] == 'queued'
    jobs[('owner', 'job-1')]['status'] = 'running'
    assert status(reloaded, tokens['owner']).get_json()['status'] == 'running'
    jobs[('owner', 'job-1')].update(status='done', result={'photosDeleted': 12, 'blobsDeleted': 24})
    done = status(reloaded, tokens['owner']).get_json()
    assert done['status'] == 'done'
    assert done['result']['photosDeleted'] == 12
    assert store.get_clean_confirmation(tokens['owner'], account_id='owner', library_id='owner')[1]['RowKey'] == request_id
    assert reloaded.post('/api/library/clean/confirm', json={'token': tokens['owner']}, headers=headers()).status_code == 400
    assert len(enqueued) == 1


def test_both_approvers_can_resume_waiting_and_started_status(cleanup_flow):
    store, _jobs, enqueued = cleanup_flow
    _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner', 'bob'])
    client = app.app.test_client()
    assert client.post('/api/library/clean/confirm', json={'token': tokens['owner']}, headers=headers()).get_json()['status'] == 'awaiting_more_approvals'
    assert status(client, tokens['owner']).get_json()['status'] == 'awaiting_more_approvals'
    assert status(client, tokens['bob'], headers=headers('bob')).get_json()['status'] == 'pending'
    assert not enqueued
    assert client.post('/api/library/clean/confirm', json={'token': tokens['bob']}, headers=headers('bob')).status_code == 200
    for user in ('owner', 'bob'):
        assert status(client, tokens[user], headers=headers(user)).get_json()['jobId'] == 'job-1'
    assert len(enqueued) == 1


def test_status_is_bound_to_the_recipient_and_active_library(cleanup_flow):
    store, _jobs, enqueued = cleanup_flow
    _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'])
    client = app.app.test_client()
    assert status(client, tokens['owner'], headers=headers('bob')).status_code == 403
    assert status(client, tokens['owner'], headers=headers('owner', 'bob')).status_code == 403
    assert client.get('/api/library/clean/confirmation-status', query_string={'token': tokens['owner']}).status_code == 401
    assert status(client, 'unknown-token').status_code == 410
    assert not enqueued


def test_started_status_remains_readable_after_approval_expiry(cleanup_flow, monkeypatch):
    store, jobs, _enqueued = cleanup_flow
    now = [10000]
    monkeypatch.setattr(library_utils, '_now', lambda: now[0])
    _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'], ttl_seconds=1)
    client = app.app.test_client()
    assert client.post('/api/library/clean/confirm', json={'token': tokens['owner']}, headers=headers()).status_code == 200
    now[0] += 10000
    jobs[('owner', 'job-1')].update(status='failed', error='Cleanup worker failed')
    result = status(client, tokens['owner']).get_json()
    assert result['status'] == 'failed'
    assert result['error'] == 'Cleanup worker failed'


def test_old_links_remain_bound_to_their_original_job(cleanup_flow):
    store, jobs, _enqueued = cleanup_flow
    client = app.app.test_client()
    links = []
    for _ in range(2):
        _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'])
        links.append(tokens['owner'])
        assert client.post('/api/library/clean/confirm', json={'token': links[-1]}, headers=headers()).status_code == 200
    jobs[('owner', 'job-1')].update(status='done', result={'photosDeleted': 5})
    assert status(client, links[0]).get_json()['jobId'] == 'job-1'
    assert status(client, links[1]).get_json()['jobId'] == 'job-2'
    assert store.get_library('owner')['lastCleanupStatus'] == 'in_progress'
    assert store.get_library('owner')['lastCleanupJobId'] == 'job-2'


def test_expired_unconfirmed_link_never_offers_new_approval(cleanup_flow):
    store, _jobs, enqueued = cleanup_flow
    _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'], ttl_seconds=-1)
    client = app.app.test_client()
    assert status(client, tokens['owner']).status_code == 410
    assert client.post('/api/library/clean/confirm', json={'token': tokens['owner']}, headers=headers()).status_code == 400
    assert not enqueued


def test_reading_recorded_approval_does_not_enqueue_when_start_is_still_pending(cleanup_flow):
    store, _jobs, enqueued = cleanup_flow
    _request, tokens = store.create_clean_request(library_id='owner', requested_by='owner', required_user_ids=['owner'])
    assert store.confirm_clean_token(tokens['owner'], account_id='owner', library_id='owner')[0] == 'ok'
    result = status(app.app.test_client(), tokens['owner'])
    assert result.get_json() == {'status': 'awaiting_start'}
    assert not enqueued
