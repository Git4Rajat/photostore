"""Delete-everything password check: core verifies the password and returns a signed proof;
the archive's clean request only accepts that proof."""
import pytest

import app
import password_auth


class FakeStore:
    def __init__(self):
        self.users = {
            'u1': {'RowKey': 'u1', 'email': 'me@example.com', 'passwordHash': password_auth.hash_password('correct horse')},
        }
        self.audits = []

    def get_user(self, uid):
        return self.users.get(uid)

    def get_active_clean_request(self, library_id):
        return None

    def clean_request_send_allowed(self, library_id):
        return True

    def list_library_members(self, library_id):
        return [{'userId': 'u1', 'isOwner': True}]

    def create_clean_request(self, **kwargs):
        return 'req-1', {'u1': 'raw-token'}

    def get_library(self, library_id):
        return {'name': 'Family'}

    def audit(self, *args, **kwargs):
        self.audits.append(kwargs)

    def cancel_clean_request(self, *args, **kwargs):
        pass


@pytest.fixture
def http(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(app, 'library_store', store)
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app, '_require_owner_context', lambda require_auth=True: ('u1', 'lib1', None))
    monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
    monkeypatch.setattr(app.email_utils, 'send_library_clean_email', lambda *a, **k: None)
    monkeypatch.setattr(app.email_utils, 'masked_recipient', lambda email: 'm•••@example.com')
    monkeypatch.setattr(password_auth, 'login_attempt_allowed', lambda table, row_key=None: True)
    monkeypatch.setattr(password_auth, 'record_login_failure', lambda table, row_key=None, **k: None)
    monkeypatch.setattr(password_auth, 'record_login_success', lambda table, row_key=None: None)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()


def test_verify_password_returns_a_proof_the_request_accepts(http):
    verified = http.post('/api/library/verify-password', json={'password': 'correct horse'})
    assert verified.status_code == 200
    proof = verified.json['proof']
    assert app._library_clean_reauth_valid(proof, 'u1', 'lib1')

    sent = http.post('/api/library/clean/request', json={'proof': proof})
    assert sent.status_code == 200
    assert sent.json['sentTo'] == ['m•••@example.com']


def test_wrong_password_gives_no_proof(http):
    resp = http.post('/api/library/verify-password', json={'password': 'nope'})
    assert resp.status_code == 401
    assert 'proof' not in resp.json


def test_verify_password_is_throttled(http, monkeypatch):
    monkeypatch.setattr(password_auth, 'login_attempt_allowed', lambda table, row_key=None: False)
    assert http.post('/api/library/verify-password', json={'password': 'correct horse'}).status_code == 429


def test_request_without_a_proof_is_refused(http):
    for body in ({}, {'proof': ''}, {'proof': 'garbage'}, {'password': 'correct horse'}):
        resp = http.post('/api/library/clean/request', json=body)
        assert resp.status_code == 401
        assert resp.json['code'] == 'reauth_required'


def test_proof_is_bound_to_account_library_and_time(http, monkeypatch):
    proof = app._library_clean_reauth_proof('u1', 'lib1')
    assert not app._library_clean_reauth_valid(proof, 'u2', 'lib1')
    assert not app._library_clean_reauth_valid(proof, 'u1', 'lib2')
    expired = app._library_clean_reauth_proof('u1', 'lib1', expires_at=1)
    assert not app._library_clean_reauth_valid(expired, 'u1', 'lib1')
    assert http.post('/api/library/clean/request', json={'proof': expired}).status_code == 401


def test_verify_password_only_exists_in_password_mode(http, monkeypatch):
    monkeypatch.setattr(app, 'AUTH_MODE', 'entra')
    assert http.post('/api/library/verify-password', json={'password': 'x'}).status_code == 400
