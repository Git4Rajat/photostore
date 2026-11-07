"""Explicit sign-out revokes one session; sign-out-everywhere voids all of them."""
import time

import pytest

import app
import library_utils
import password_auth


class _Users:
    def __init__(self, rows):
        self.rows = rows

    def get_entity(self, partition_key, row_key):
        return dict(self.rows[row_key])

    def upsert_entity(self, entity):
        self.rows[entity['RowKey']] = dict(entity)


@pytest.fixture
def store(monkeypatch):
    s = library_utils.LibraryStore.__new__(library_utils.LibraryStore)
    s.users = _Users({'u1': {'PartitionKey': library_utils._USER_PK, 'RowKey': 'u1', 'email': 'me@example.com', 'tokenVersion': 1}})
    monkeypatch.setattr(s, 'get_user', lambda uid: s.users.rows.get(uid) and dict(s.users.rows[uid]), raising=False)
    monkeypatch.setattr(s, 'get_user_checked', s.get_user, raising=False)
    monkeypatch.setattr(app, 'library_store', s)
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app, 'SESSION_SECRET', 'test-secret-test-secret-test-secret')
    return s


def _token(ver=1):
    return password_auth.issue_session_token('test-secret-test-secret-test-secret', user_id='u1', library_id='u1',
                                             token_version=ver, ttl_seconds=3600)


def _get(token):
    return app.app.test_client().post('/api/auth/logout', headers={'Authorization': f'Bearer {token}'})


def test_tokens_carry_unique_session_ids():
    a = password_auth.validate_session_token('s' * 32, password_auth.issue_session_token('s' * 32, user_id='u', library_id='u', token_version=1, ttl_seconds=60))
    b = password_auth.validate_session_token('s' * 32, password_auth.issue_session_token('s' * 32, user_id='u', library_id='u', token_version=1, ttl_seconds=60))
    assert a['jti'] and a['jti'] != b['jti']


def test_logout_revokes_only_that_session(store):
    t1, t2 = _token(), _token()
    assert _get(t1).status_code == 200
    assert _get(t1).status_code == 401  # revoked
    assert _get(t2).status_code == 200  # other device unaffected


def test_logout_all_voids_every_session(store):
    t1, t2 = _token(), _token()
    r = app.app.test_client().post('/api/auth/logout-all', headers={'Authorization': f'Bearer {t1}'})
    assert r.status_code == 200
    assert _get(t2).status_code == 401


def test_revocation_list_is_pruned_and_bounded(store):
    now = int(time.time())
    store.revoke_session('u1', 'old', now - 10)
    for i in range(store._MAX_REVOKED_SESSIONS + 20):
        store.revoke_session('u1', f'j{i}', now + 1000 + i)
    listed = store._revoked_sessions(store.get_user('u1'))
    assert 'old' not in listed and len(listed) == store._MAX_REVOKED_SESSIONS
    assert f'j{store._MAX_REVOKED_SESSIONS + 19}' in listed  # newest kept


def test_anonymous_rate_limit_is_per_ip_per_bucket_and_resets(monkeypatch):
    monkeypatch.setattr(app, '_ANON_LIMITS', ((('/api/auth/',), 'auth', 3),))
    monkeypatch.setattr(app, '_anon_counters', {})
    assert [app._anon_rate_limited('/api/auth/login', '1.1.1.1', 100.0) for _ in range(3)] == [None] * 3
    assert app._anon_rate_limited('/api/auth/login', '1.1.1.1', 101.0) == 59
    assert app._anon_rate_limited('/api/auth/login', '2.2.2.2', 101.0) is None   # other client
    assert app._anon_rate_limited('/api/albums', '1.1.1.1', 101.0) is None        # not an anonymous route
    assert app._anon_rate_limited('/api/auth/login', '1.1.1.1', 161.0) is None    # new window


def test_anonymous_rate_limit_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(app, '_ANON_LIMITS', ((('/api/auth/',), 'auth', 1),))
    monkeypatch.setattr(app, '_anon_counters', {})
    c = app.app.test_client()
    c.get('/api/auth/config')
    r = c.get('/api/auth/config')
    assert r.status_code == 429 and int(r.headers['Retry-After']) >= 1
