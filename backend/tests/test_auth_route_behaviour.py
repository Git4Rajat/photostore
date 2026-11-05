"""Auth routes: config, password login/throttle, change/forgot/reset password, Entra exchange."""
import pytest

import app
import password_auth


class FakeLibraryStore:
    def __init__(self):
        self.users = {}
        self.reset_tokens = {}
        self.versions = {}
        self.passwords = {}
        self.reset_allowed = True
        self.created_resets = []

    def add_user(self, uid, email, password):
        self.users[uid] = {'RowKey': uid, 'email': email, 'passwordHash': password_auth.hash_password(password)}

    def get_user_by_email(self, email):
        return next((u for u in self.users.values() if u['email'] == email), None)

    def get_user(self, uid):
        return self.users.get(uid)

    def get_current_library(self, uid):
        return 'lib-' + uid

    def set_user_password(self, uid, hashed):
        self.passwords[uid] = hashed

    def bump_token_version(self, uid):
        self.versions[uid] = self.versions.get(uid, 0) + 1

    def reset_email_allowed(self, uid):
        return self.reset_allowed

    def create_reset_token(self, uid, ttl_seconds):
        self.created_resets.append((uid, ttl_seconds))
        return 'raw-token'

    def consume_reset_token(self, token):
        return self.reset_tokens.pop(token, None)


@pytest.fixture
def store(monkeypatch):
    fake = FakeLibraryStore()
    fake.add_user('u1', 'me@example.com', 'correct horse')
    monkeypatch.setattr(app, 'library_store', fake)
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app, '_issue_session_for', lambda uid, library_id=None, email='', mode='': f'tok:{uid}:{library_id}:{mode}')
    return fake


@pytest.fixture
def throttle(monkeypatch):
    state = {'allowed': True, 'failures': [], 'successes': []}
    monkeypatch.setattr(password_auth, 'login_attempt_allowed', lambda table, row_key=None: state['allowed'])
    monkeypatch.setattr(password_auth, 'record_login_failure', lambda table, row_key=None, **k: state['failures'].append(row_key))
    monkeypatch.setattr(password_auth, 'record_login_success', lambda table, row_key=None: state['successes'].append(row_key))
    return state


@pytest.fixture
def http():
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()


def test_config_reports_mode_and_reset_availability(http, monkeypatch):
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
    body = http.get('/api/auth/config').json
    assert body['authMode'] == 'password' and body['passwordResetAvailable'] is True
    monkeypatch.setattr(app, 'AUTH_MODE', 'entra')
    assert http.get('/api/auth/config').json['passwordResetAvailable'] is False


@pytest.mark.parametrize('path,body', [
    ('/api/auth/login', {'email': 'a@b.c', 'password': 'x'}),
    ('/api/auth/change-password', {}),
    ('/api/auth/forgot', {'email': 'a@b.c'}),
    ('/api/auth/reset', {'token': 't', 'newPassword': 'longenough'}),
])
def test_password_endpoints_are_closed_in_entra_mode(http, monkeypatch, path, body):
    monkeypatch.setattr(app, 'AUTH_MODE', 'entra')
    assert http.post(path, json=body).status_code == 400


class TestLogin:
    URL = '/api/auth/login'

    def test_requires_email_and_password(self, http, store, throttle):
        assert http.post(self.URL, json={'email': 'me@example.com'}).status_code == 400
        assert http.post(self.URL, json={'password': 'x'}).status_code == 400

    def test_success_issues_a_session_for_the_current_library_and_clears_the_throttle(self, http, store, throttle):
        body = http.post(self.URL, json={'email': ' ME@Example.com ', 'password': 'correct horse'}).json
        assert body['token'] == 'tok:u1:lib-u1:password' and body['email'] == 'me@example.com'
        assert body['expiresIn'] == app.SESSION_TTL_SECONDS
        assert throttle['successes'] == ['login-throttle:me@example.com'] and throttle['failures'] == []

    def test_wrong_password_and_unknown_email_are_indistinguishable_and_recorded(self, http, store, throttle):
        wrong = http.post(self.URL, json={'email': 'me@example.com', 'password': 'nope'})
        unknown = http.post(self.URL, json={'email': 'who@example.com', 'password': 'nope'})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json == unknown.json
        assert throttle['failures'] == ['login-throttle:me@example.com', 'login-throttle:who@example.com']

    def test_throttled_email_is_rejected_even_with_the_right_password(self, http, store, throttle):
        throttle['allowed'] = False
        response = http.post(self.URL, json={'email': 'me@example.com', 'password': 'correct horse'})
        assert response.status_code == 429 and 'token' not in response.json

    def test_account_without_a_password_hash_cannot_sign_in(self, http, store, throttle):
        store.users['u1']['passwordHash'] = ''
        assert http.post(self.URL, json={'email': 'me@example.com', 'password': ''}).status_code == 400
        assert http.post(self.URL, json={'email': 'me@example.com', 'password': 'x'}).status_code == 401


class TestChangePassword:
    URL = '/api/auth/change-password'

    @pytest.fixture(autouse=True)
    def signed_in(self, monkeypatch, store):
        monkeypatch.setattr(app, '_require_library_context', lambda **k: ('u1', 'shared-lib', None))

    def test_rejects_short_new_password(self, http, store):
        assert http.post(self.URL, json={'currentPassword': 'correct horse', 'newPassword': 'short'}).status_code == 400
        assert store.passwords == {}

    def test_rejects_wrong_current_password_without_changing_anything(self, http, store):
        assert http.post(self.URL, json={'currentPassword': 'wrong', 'newPassword': 'brand new pw'}).status_code == 401
        assert store.passwords == {} and store.versions == {}

    def test_changes_password_revokes_old_sessions_and_stays_in_the_active_library(self, http, store):
        body = http.post(self.URL, json={'currentPassword': 'correct horse', 'newPassword': 'brand new pw'}).json
        assert body['status'] == 'ok' and body['token'] == 'tok:u1:shared-lib:password'
        assert password_auth.verify_password('brand new pw', store.passwords['u1'])
        assert store.versions == {'u1': 1}

    def test_auth_failure_is_passed_through(self, http, monkeypatch, store):
        monkeypatch.setattr(app, '_require_library_context', lambda **k: (None, None, (app.jsonify({'error': 'no'}), 401)))
        assert http.post(self.URL, json={'currentPassword': 'x', 'newPassword': 'long enough'}).status_code == 401


class TestForgotPassword:
    URL = '/api/auth/forgot'

    @pytest.fixture
    def mail(self, monkeypatch):
        sent = []
        monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
        monkeypatch.setattr(app.email_utils, 'send_password_reset_email', lambda to, url: sent.append((to, url)))
        monkeypatch.setattr(app, 'PUBLIC_APP_BASE_URL', 'https://photos.example.com/')
        return sent

    def test_known_account_gets_a_reset_link(self, http, store, mail):
        assert http.post(self.URL, json={'email': 'me@example.com'}).json == {'status': 'ok'}
        assert mail == [('me@example.com', 'https://photos.example.com/reset-password?token=raw-token')]
        assert store.created_resets == [('u1', 3600)]

    def test_response_never_reveals_whether_the_account_exists(self, http, store, mail, monkeypatch):
        unknown = http.post(self.URL, json={'email': 'ghost@example.com'})
        throttled = (setattr(store, 'reset_allowed', False), http.post(self.URL, json={'email': 'me@example.com'}))[1]
        monkeypatch.setattr(app.email_utils, 'is_configured', lambda: False)
        unconfigured = http.post(self.URL, json={'email': 'me@example.com'})
        no_email = http.post(self.URL, json={})
        assert all(r.status_code == 200 and r.json == {'status': 'ok'} for r in (unknown, throttled, unconfigured, no_email))
        assert mail == [] and store.created_resets == []

    def test_mail_failure_is_swallowed(self, http, store, mail, monkeypatch):
        def boom(*a):
            raise RuntimeError('acs down')

        monkeypatch.setattr(app.email_utils, 'send_password_reset_email', boom)
        assert http.post(self.URL, json={'email': 'me@example.com'}).json == {'status': 'ok'}


class TestResetPassword:
    URL = '/api/auth/reset'

    def test_rejects_short_password_before_consuming_the_token(self, http, store):
        store.reset_tokens['t1'] = 'u1'
        assert http.post(self.URL, json={'token': 't1', 'newPassword': 'short'}).status_code == 400
        assert 't1' in store.reset_tokens

    def test_invalid_token_is_rejected(self, http, store):
        assert http.post(self.URL, json={'token': 'bad', 'newPassword': 'long enough pw'}).status_code == 400
        assert store.passwords == {}

    def test_valid_token_sets_password_revokes_sessions_and_is_single_use(self, http, store):
        store.reset_tokens['t1'] = 'u1'
        assert http.post(self.URL, json={'token': 't1', 'newPassword': 'long enough pw'}).json == {'status': 'ok'}
        assert password_auth.verify_password('long enough pw', store.passwords['u1']) and store.versions == {'u1': 1}
        assert http.post(self.URL, json={'token': 't1', 'newPassword': 'another long pw'}).status_code == 400


class TestEntraExchange:
    URL = '/api/auth/exchange'

    @pytest.fixture(autouse=True)
    def entra(self, monkeypatch, store):
        monkeypatch.setattr(app, 'AUTH_MODE', 'entra')
        self.bootstrapped = []
        monkeypatch.setattr(app, '_ensure_account_bootstrapped', lambda uid, email='': self.bootstrapped.append((uid, email)))

    def test_requires_a_bearer_token(self, http):
        assert http.post(self.URL).status_code == 401
        assert http.post(self.URL, headers={'Authorization': 'Basic abc'}).status_code == 401

    def test_rejects_an_invalid_microsoft_token(self, http, monkeypatch):
        def boom(*a):
            raise ValueError('bad signature')

        monkeypatch.setattr(app, 'validate_entra_bearer_token', boom)
        response = http.post(self.URL, headers={'Authorization': 'Bearer x'})
        assert response.status_code == 401 and 'bad signature' not in response.get_data(as_text=True)

    def test_rejects_a_token_without_a_user_identifier(self, http, monkeypatch):
        monkeypatch.setattr(app, 'validate_entra_bearer_token', lambda *a: {'name': 'No Id'})
        assert http.post(self.URL, headers={'Authorization': 'bearer x'}).status_code == 401
        assert self.bootstrapped == []

    def test_exchanges_for_a_session_preferring_oid_and_bootstraps_the_account(self, http, monkeypatch):
        monkeypatch.setattr(app, 'validate_entra_bearer_token',
                            lambda *a: {'oid': 'oid-1', 'sub': 'sub-1', 'preferred_username': 'ann@corp.com'})
        body = http.post(self.URL, headers={'Authorization': 'Bearer ms-token'}).json
        assert body['token'] == 'tok:oid-1:lib-oid-1:entra' and body['email'] == 'ann@corp.com'
        assert self.bootstrapped == [('oid-1', 'ann@corp.com')]

    def test_falls_back_to_sub_for_the_user_id(self, http, monkeypatch):
        monkeypatch.setattr(app, 'validate_entra_bearer_token', lambda *a: {'sub': 'sub-1', 'email': 'a@b.c'})
        assert http.post(self.URL, headers={'Authorization': 'Bearer t'}).json['token'].startswith('tok:sub-1:')


def test_exchange_is_unavailable_outside_entra_mode(http, store):
    assert http.post('/api/auth/exchange', headers={'Authorization': 'Bearer t'}).status_code == 400
