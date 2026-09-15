"""Unit tests for the unified Photostore session token (both auth modes)."""
import time

import jwt
import pytest

import password_auth

SECRET = 'test-secret'


def test_issue_and_validate_round_trip():
    token = password_auth.issue_session_token(
        SECRET, user_id='ubob', library_id='owner', token_version=3,
        email='bob@x.com', mode='entra', ttl_seconds=3600,
    )
    payload = password_auth.validate_session_token(SECRET, token)
    assert payload['sub'] == 'ubob'
    assert payload['lib'] == 'owner'      # active library differs from account
    assert payload['ver'] == 3
    assert payload['mode'] == 'entra'
    assert payload['email'] == 'bob@x.com'


def test_default_active_library_is_own():
    token = password_auth.issue_session_token(
        SECRET, user_id='owner', library_id='owner', token_version=1,
        ttl_seconds=3600,
    )
    assert password_auth.validate_session_token(SECRET, token)['lib'] == 'owner'


def test_expired_token_rejected():
    token = password_auth.issue_session_token(
        SECRET, user_id='owner', library_id='owner', token_version=1,
        ttl_seconds=-1,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        password_auth.validate_session_token(SECRET, token)


def test_wrong_secret_rejected():
    token = password_auth.issue_session_token(
        SECRET, user_id='owner', library_id='owner', token_version=1,
        ttl_seconds=3600,
    )
    with pytest.raises(Exception):
        password_auth.validate_session_token('other-secret', token)
