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


def test_new_account_own_library_invite_names_the_library(management):
    store, client = management
    token = store.create_invite(library_id='owner', email='neha@example.com', target_type='fresh', invited_by='owner')
    accepted = client.post('/api/library/invite/accept', json={'token': token, 'password': 'long-enough-1', 'libraryName': '  Neha family  '})
    assert accepted.status_code == 200
    neha = store.get_user_by_email('neha@example.com')['RowKey']
    assert store.get_library(neha)['name'] == 'Neha family'


def test_new_account_invite_library_name_defaults_to_email_and_is_ignored_when_joining(management):
    store, client = management
    fresh = store.create_invite(library_id='owner', email='a@example.com', target_type='fresh', invited_by='owner')
    assert client.post('/api/library/invite/accept', json={'token': fresh, 'password': 'long-enough-1'}).status_code == 200
    assert store.get_library(store.get_user_by_email('a@example.com')['RowKey'])['name'] == 'a@example.com'
    join = store.create_invite(library_id='owner', email='b@example.com', target_type='join', invited_by='owner')
    assert client.post('/api/library/invite/accept', json={'token': join, 'password': 'long-enough-1', 'libraryName': 'Nope'}).status_code == 200
    assert store.get_library(store.get_user_by_email('b@example.com')['RowKey'])['name'] == 'b@example.com'


def _transfer(client, store, proof=True, **body):
    owner = headers('owner')
    if proof:
        body['proof'] = app._library_clean_reauth_proof('owner', 'owner')
    return client.post('/api/library/transfer', headers=owner, json=body)


def test_transfer_hands_library_over_and_old_owner_gets_a_new_one(management):
    store, client = management
    store.add_membership('bob', 'owner')
    assert _transfer(client, store, userId='bob').status_code == 200
    assert store.is_owner('bob', 'owner') and not store.is_owner('owner', 'owner')
    assert store.library_owner_id('owner') == 'bob'
    own = store.personal_library_id('owner')
    assert own != 'owner' and store.is_owner('owner', own)
    # Signing in again must not hand the old owner the library back.
    store.ensure_personal_library('owner')
    assert not store.is_owner('owner', 'owner')
    assert store.get_current_library('owner') == own
    # Old owner can now leave; the new owner can't.
    left = client.post('/api/library/leave', headers=headers('owner', 'owner'), json={'libraryId': 'owner'})
    assert left.status_code == 200 and left.get_json()['activeLibraryId'] == own
    assert client.post('/api/library/leave', headers=headers('bob', 'owner'), json={'libraryId': 'owner'}).status_code == 400
    # The new owner has owner powers immediately.
    assert client.post('/api/library/rename', headers=headers('bob', 'owner'), json={'name': 'Bob'}).status_code == 200


def test_transfer_guards(management):
    store, client = management
    store.add_membership('bob', 'owner')
    assert _transfer(client, store, userId='outsider').status_code == 404
    assert _transfer(client, store, userId='owner').status_code == 400
    assert _transfer(client, store, proof=False, userId='bob').status_code == 401
    member = client.post('/api/library/transfer', headers=headers('bob', 'owner'), json={'userId': 'bob'})
    assert member.status_code == 403
    assert store.is_owner('owner', 'owner')


def test_transfer_retry_after_partial_failure_converges(management):
    store, client = management
    store.add_membership('bob', 'owner')
    first = store.transfer_ownership('owner', 'owner', 'bob')
    # Simulate a crash between promoting bob and demoting the old owner.
    store.add_membership('owner', 'owner', is_owner=True)
    second = store.transfer_ownership('owner', 'owner', 'bob')
    assert first == second
    assert store.is_owner('bob', 'owner') and not store.is_owner('owner', 'owner')
