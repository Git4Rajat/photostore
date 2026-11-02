import pytest

import app
from fakes import make_store


@pytest.fixture
def management(monkeypatch):
    store = make_store()
    store.require_invite_index = True
    for user in ('owner', 'bob', 'outsider'):
        store.create_user(email=f'{user}@example.com', user_id=user, password_hash='unused')
        store.ensure_personal_library(user)
    monkeypatch.setattr(app, 'library_store', store)
    monkeypatch.setattr(app, 'SESSION_SECRET', 'management-test-secret-with-at-least-32-bytes')
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
    monkeypatch.setattr(app.email_utils, 'send_invite_email', lambda *_a, **_k: None)
    return store, app.app.test_client()


def headers(user, library=None):
    return {'Authorization': f'Bearer {app._issue_session_for(user, library_id=library or user)}'}


def test_management_sessions_and_immediate_revocation(management):
    store, client = management
    owner = headers('owner')
    assert client.post('/api/library/rename', headers=owner, json={'name': 'Family'}).status_code == 200
    assert client.post('/api/library/invite', headers=owner, json={'email': 'bob@example.com'}).status_code == 200
    pending = client.get('/api/library/members', headers=owner).get_json()['pendingInvites']
    assert len(pending) == 1
    token = store.create_invite(library_id='owner', email='outsider@example.com', target_type='join', invited_by='owner')
    assert client.get(f'/api/library/invite/info?token={token}').get_json()['valid'] is True
    # Token possession alone cannot accept an invitation for an existing account.
    assert client.post('/api/library/invite/accept', json={'token': token}).status_code == 401
    assert client.post('/api/library/invite/accept', headers=headers('bob'), json={'token': token}).status_code == 403
    accepted = client.post('/api/library/invite/accept', headers=headers('outsider'), json={'token': token})
    assert accepted.status_code == 200
    joined = {'Authorization': f"Bearer {accepted.get_json()['token']}"}
    assert client.get('/api/library/mine', headers=joined).get_json()['activeLibraryId'] == 'owner'
    assert client.post('/api/library/rename', headers=joined, json={'name': 'No'}).status_code == 403
    assert client.post('/api/library/members/remove', headers=owner, json={'userId': 'outsider'}).status_code == 200
    assert client.get('/api/library/members', headers=joined).status_code == 403
    assert client.get('/api/library/mine', headers=headers('outsider')).status_code == 200
    assert client.post('/api/library/switch', headers=headers('bob'), json={'libraryId': 'owner'}).status_code == 403
    assert client.post('/api/library/invite/revoke', headers=owner, json={'inviteId': pending[0]['inviteId']}).status_code == 200
    assert client.get('/api/library/members', headers=owner).get_json()['pendingInvites'] == []


def test_switch_and_leave_reissue_usable_sessions(management):
    store, client = management
    store.add_membership('bob', 'owner')
    switched = client.post('/api/library/switch', headers=headers('bob'), json={'libraryId': 'owner'})
    assert switched.status_code == 200
    joined = {'Authorization': f"Bearer {switched.get_json()['token']}"}
    assert client.get('/api/library/mine', headers=joined).get_json()['activeLibraryId'] == 'owner'
    left = client.post('/api/library/leave', headers=joined, json={'libraryId': 'owner'})
    assert left.status_code == 200
    personal = {'Authorization': f"Bearer {left.get_json()['token']}"}
    assert client.get('/api/library/mine', headers=personal).get_json()['activeLibraryId'] == 'bob'
    assert client.get('/api/library/members', headers=joined).status_code == 403


def test_core_returns_retryable_error_before_invite_migration(management, monkeypatch):
    store, client = management
    store.invites.delete_entity('owner', '_pending_index_v1')
    def no_scan(*_a, **_k):
        pytest.fail('core must not scan invitation history')
    monkeypatch.setattr(store.invites, 'query_entities', no_scan)
    response = client.get('/api/library/members', headers=headers('owner'))
    assert response.status_code == 503
    assert 'temporarily unavailable' in response.get_json()['error']


def test_cleanup_snapshot_does_not_reconcile_or_enqueue(management, monkeypatch):
    store, client = management
    store.set_cleanup_completed('owner', 12, 24)
    monkeypatch.setattr(app, '_reconcile_stale_library_cleanup', lambda *_a, **_k: pytest.fail('core reconciled cleanup'))
    for path in ('/api/library/mine', '/api/library/members'):
        response = client.get(path, headers=headers('owner'))
        assert response.status_code == 200
        assert response.get_json()['cleanupInfo']['lastCleanupPhotosDeleted'] == 12
