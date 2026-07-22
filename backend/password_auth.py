"""Single-owner password authentication for Photostore.

This is the "simple" auth mode aimed at a non-technical owner who deploys
Photostore as their own private photo backup. Instead of enterprise SSO
(Microsoft Entra), the owner sets one email + password at deploy time and logs
in with it. Password recovery is handled via an emailed reset link.

Design notes:
  * One identity. Every authenticated request maps to a single fixed user id
    (`OWNER_USER_ID`), so all existing per-user data (partitioned by user id)
    belongs to the owner.
  * The password is stored only as a salted scrypt hash (stdlib `hashlib`,
    no extra dependency) in Table Storage — never in plaintext, never in the
    session token.
  * Sessions are stateless signed tokens (JWT, HS256) so the backend stays
    horizontally scalable (backend + worker share nothing).
  * Reset tokens are single-use, short-lived, and stored only as a hash.

The functions here are storage-agnostic: callers pass in an
`azure.data.tables` table client so this module has no global state and is easy
to test.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Dict, Optional, Tuple

import jwt

# The single identity every password-mode request resolves to.
OWNER_USER_ID = 'owner'

# Table layout: one partition, well-known row keys.
_AUTH_PARTITION = 'auth'
_OWNER_ROW = 'owner'
_RESET_ROW = 'owner-reset'

# scrypt parameters (interactive-login appropriate; ~tens of ms).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

_SESSION_ALGO = 'HS256'


# ---------------------------------------------------------------------------
# Password hashing (scrypt via hashlib — no third-party dependency)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Return an encoded scrypt hash: ``scrypt$N$r$p$salthex$hashhex``."""
    if not password:
        raise ValueError('Password must not be empty.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode('utf-8'), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f'scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verify ``password`` against an encoded scrypt hash."""
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = encoded.split('$')
        if scheme != 'scrypt':
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.scrypt(
        (password or '').encode('utf-8'), salt=salt,
        n=n, r=r, p=p, dklen=len(expected),
    )
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------------------
# Owner credential storage
# ---------------------------------------------------------------------------
def get_owner_credential(table_client) -> Optional[Dict]:
    """Return the stored owner credential entity, or None if not seeded yet."""
    try:
        return table_client.get_entity(partition_key=_AUTH_PARTITION, row_key=_OWNER_ROW)
    except Exception:
        return None


def owner_email(table_client) -> str:
    entity = get_owner_credential(table_client) or {}
    return str(entity.get('email', '') or '')


def set_owner_password(table_client, password: str, email: Optional[str] = None) -> None:
    """Create or update the owner credential."""
    existing = get_owner_credential(table_client) or {}
    entity = {
        'PartitionKey': _AUTH_PARTITION,
        'RowKey': _OWNER_ROW,
        'passwordHash': hash_password(password),
        'email': (email if email is not None else existing.get('email', '')) or '',
        'updatedAt': int(time.time()),
    }
    table_client.upsert_entity(entity)


def seed_owner_if_missing(table_client, email: str, password: str) -> bool:
    """Seed the initial owner credential on first boot. Returns True if seeded.

    No-op (returns False) if a credential already exists, so redeploys or
    restarts never clobber a password the owner has since changed.
    """
    if get_owner_credential(table_client) is not None:
        return False
    if not password:
        return False
    set_owner_password(table_client, password, email=email or '')
    return True


def verify_owner_password(table_client, password: str) -> bool:
    entity = get_owner_credential(table_client)
    if not entity:
        return False
    return verify_password(password, str(entity.get('passwordHash', '') or ''))


# ---------------------------------------------------------------------------
# Session tokens (stateless, signed)
# ---------------------------------------------------------------------------
def issue_session_token(secret: str, email: str, ttl_seconds: int) -> str:
    if not secret:
        raise RuntimeError('SESSION_SECRET is not configured.')
    now = int(time.time())
    payload = {
        'sub': OWNER_USER_ID,
        'email': email or '',
        'iat': now,
        'exp': now + int(ttl_seconds),
        'mode': 'password',
    }
    return jwt.encode(payload, secret, algorithm=_SESSION_ALGO)


def validate_session_token(secret: str, token: str) -> Dict:
    if not secret:
        raise RuntimeError('SESSION_SECRET is not configured.')
    payload = jwt.decode(
        token, secret, algorithms=[_SESSION_ALGO],
        options={'require': ['exp', 'iat', 'sub']},
    )
    if payload.get('mode') != 'password' or payload.get('sub') != OWNER_USER_ID:
        raise RuntimeError('Not a valid owner session token.')
    return payload


# ---------------------------------------------------------------------------
# Password reset tokens (single-use, short-lived, stored only as a hash)
# ---------------------------------------------------------------------------
def _hash_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def create_reset_token(table_client, ttl_seconds: int = 3600) -> str:
    """Generate a reset token, persist only its hash + expiry, return the raw token."""
    raw = secrets.token_urlsafe(32)
    entity = {
        'PartitionKey': _AUTH_PARTITION,
        'RowKey': _RESET_ROW,
        'tokenHash': _hash_reset_token(raw),
        'expiresAt': int(time.time()) + int(ttl_seconds),
    }
    table_client.upsert_entity(entity)
    return raw


def consume_reset_token(table_client, raw: str) -> bool:
    """Validate a reset token and invalidate it (single use)."""
    if not raw:
        return False
    try:
        entity = table_client.get_entity(partition_key=_AUTH_PARTITION, row_key=_RESET_ROW)
    except Exception:
        return False
    stored_hash = str(entity.get('tokenHash', '') or '')
    expires_at = int(entity.get('expiresAt', 0) or 0)
    valid = (
        bool(stored_hash)
        and time.time() < expires_at
        and hmac.compare_digest(stored_hash, _hash_reset_token(raw))
    )
    # Always delete on any consume attempt against a present token to prevent
    # brute-forcing; a fresh token must be requested if this one was wrong.
    try:
        table_client.delete_entity(partition_key=_AUTH_PARTITION, row_key=_RESET_ROW)
    except Exception:
        pass
    return valid


def reset_password_with_token(table_client, raw_token: str, new_password: str) -> bool:
    if not consume_reset_token(table_client, raw_token):
        return False
    set_owner_password(table_client, new_password)
    return True


def resolve_password_user_id(headers, secret: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the owner identity from a session bearer token.

    Returns (user_id, None) on success or (None, error_message) on failure.
    """
    auth_header = str(headers.get('Authorization', '') or '')
    if not auth_header.lower().startswith('bearer '):
        return None, 'Authorization token is required.'
    token = auth_header.split(' ', 1)[1].strip()
    try:
        validate_session_token(secret, token)
        return OWNER_USER_ID, None
    except Exception as exc:
        return None, f'Invalid or expired session: {exc}'
