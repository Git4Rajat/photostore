"""Owner sign-in on a fresh deployment: the owner account is created with the deploy-time password even when the
first boot can't finish, is never created without one, and a password-less account an older image left behind is
repaired. 2026-11-14: fresh installs had no owner account at all, so sign-in and reset emails both failed."""
from __future__ import annotations

import pytest
from azure.core.exceptions import ResourceNotFoundError

import app
import password_auth
import wave_bootstrap
from tests.fakes import FakeTable, make_store

EMAIL = 'owner@example.test'


@pytest.fixture
def boot(monkeypatch):
    store = make_store()
    config = FakeTable()
    monkeypatch.setattr(app, 'library_store', store)
    monkeypatch.setattr(app, 'config_table_client', config)
    monkeypatch.setattr(app, 'AUTH_MODE', 'password')
    monkeypatch.setattr(app, 'OWNER_EMAIL', EMAIL)
    monkeypatch.setattr(app, '_owner_retry_at', 0.0)
    monkeypatch.delenv('UPLOAD_WAVE_AUTO_BOOTSTRAP', raising=False)
    monkeypatch.delenv('UPLOAD_WAVE_BOOTSTRAP_PENDING', raising=False)
    return store, config


def _owner(store):
    return store.get_user(password_auth.OWNER_USER_ID)


def test_a_role_without_the_password_creates_no_owner_account(boot):
    # cluster and vision boot with OWNER_EMAIL but no OWNER_PASSWORD; before core has seeded the credential.
    store, _ = boot
    assert app._bootstrap_owner_account() is False
    assert _owner(store) is None


def test_the_owner_account_gets_the_seeded_password(boot):
    store, config = boot
    password_auth.seed_owner_if_missing(config, EMAIL, 'deploy-time-pass')
    assert app._bootstrap_owner_account() is True
    owner = _owner(store)
    assert password_auth.verify_password('deploy-time-pass', owner['passwordHash'])
    assert store.get_user_by_email(EMAIL)['RowKey'] == password_auth.OWNER_USER_ID


def test_a_password_less_owner_account_is_repaired_from_the_seeded_credential(boot):
    store, config = boot
    store.create_user(email=EMAIL, password_hash=None, user_id=password_auth.OWNER_USER_ID)
    password_auth.seed_owner_if_missing(config, EMAIL, 'deploy-time-pass')
    assert app._bootstrap_owner_account() is True
    assert password_auth.verify_password('deploy-time-pass', _owner(store)['passwordHash'])


def test_a_password_the_owner_changed_is_never_overwritten(boot):
    store, config = boot
    password_auth.seed_owner_if_missing(config, EMAIL, 'deploy-time-pass')
    app._bootstrap_owner_account()
    store.set_user_password(password_auth.OWNER_USER_ID, password_auth.hash_password('changed-in-app'))
    assert app._bootstrap_owner_account() is True
    assert password_auth.verify_password('changed-in-app', _owner(store)['passwordHash'])


def test_a_failed_bootstrap_reports_failure(boot, monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_AUTO_BOOTSTRAP', 'true')
    monkeypatch.setattr(app, '_wave_table_service', lambda: (_ for _ in ()).throw(RuntimeError('403')))
    assert app._bootstrap_owner_account() is False


def test_retry_creates_the_tables_seeding_needs_first(boot, monkeypatch):
    store, config = boot
    created = []
    names = ('users_table_client', 'libraries_table_client', 'memberships_table_client', 'invites_table_client',
             'audit_table_client', 'jobs_table_client', 'metadata_table_client')
    for name in names:
        table = FakeTable()
        table.create_table = lambda name=name: created.append(name)
        monkeypatch.setattr(app, name, table)
    config.create_table = lambda: created.append('config_table_client')
    monkeypatch.setattr(app, 'OWNER_PASSWORD', 'deploy-time-pass')
    assert app.retry_owner_bootstrap() is True
    assert set(created) == {'config_table_client', *names}
    assert password_auth.verify_password('deploy-time-pass', _owner(store)['passwordHash'])
    assert app.retry_owner_bootstrap() is False  # throttled to one attempt per 30 s


def test_a_sign_in_during_a_running_retry_waits_for_it(boot, monkeypatch):
    # 2 of 100 emulated deployments: the sign-in hit the throttle while the background retry was still creating
    # the account, and was refused moments before the account appeared.
    import threading
    store, config = boot
    monkeypatch.setattr(app, 'OWNER_PASSWORD', 'deploy-time-pass')
    inside, release = threading.Event(), threading.Event()
    original = app._bootstrap_owner_account

    def slow_bootstrap():
        inside.set()
        release.wait(5)
        return original()
    monkeypatch.setattr(app, '_bootstrap_owner_account', slow_bootstrap)
    background = threading.Thread(target=app.retry_owner_bootstrap)
    background.start()
    inside.wait(5)
    threading.Timer(0.2, release.set).start()
    assert app.retry_owner_bootstrap() is False  # throttled, but only after the running attempt finished
    background.join(5)
    assert store.get_user_by_email(EMAIL) is not None


def test_background_retries_stop_once_the_owner_exists(boot, monkeypatch):
    outcomes = iter([False, False, True])
    calls = []
    monkeypatch.setattr(app, 'retry_owner_bootstrap', lambda: calls.append(1) or next(outcomes))
    monkeypatch.setattr(app.time, 'sleep', lambda s: None)
    started = []
    monkeypatch.setattr(app.threading, 'Thread', lambda target, **k: started.append(target) or type(
        'T', (), {'start': lambda self: None})())
    app._retry_owner_bootstrap_in_background()
    started[0]()
    assert len(calls) == 3


def test_wave_upgrade_check_treats_a_missing_jobs_table_as_nothing_enrolled(monkeypatch):
    class Missing:
        def query_entities(self, *a, **k):
            raise ResourceNotFoundError('The table specified does not exist.')

    class Service:
        def get_table_client(self, name):
            return Missing()

    env = {'UPLOAD_WAVE_COMPATIBLE_CAPABILITIES': 'a' * 64, 'UPLOAD_WAVE_SOURCE_COMMIT': 'b' * 40}
    assert wave_bootstrap.upgrade_existing(Service(), {}, 'c' * 64, env) is False


def test_forgot_password_retries_the_owner_bootstrap(boot, monkeypatch):
    from routes import auth
    store, config = boot
    sent = []
    monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
    monkeypatch.setattr(app.email_utils, 'send_password_reset_email', lambda to, url: sent.append(to))
    monkeypatch.setattr(app, 'retry_owner_bootstrap',
                        lambda: password_auth.seed_owner_if_missing(config, EMAIL, 'p' * 10) and app._bootstrap_owner_account())
    auth._send_reset_link(EMAIL)
    assert sent == [EMAIL]


def test_forgot_password_for_another_address_does_not_retry(boot, monkeypatch):
    from routes import auth
    monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
    monkeypatch.setattr(app, 'retry_owner_bootstrap', lambda: pytest.fail('must not retry'))
    auth._send_reset_link('stranger@example.test')
