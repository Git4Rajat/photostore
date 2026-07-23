"""Multi-tenant library membership, accounts, and invites for Photostore.

Introduces ``library_id``, decoupled from ``user_id``:

  * Every user has exactly one personal library (``library_id == user_id``).
  * A "shared library" is simply a personal library that has other members.
  * All photo / face / album data is partitioned by the *active* library id,
    resolved per request from the signed session token.

This module is the single home for the account, library, membership, invite,
and audit tables. It is deliberately storage-agnostic: a :class:`LibraryStore`
is constructed with ``azure.data.tables`` table clients, so there is no global
state and it is easy to unit-test with in-memory fakes (mirroring the design of
``password_auth.py``).

Table layout
------------
users (``photousers``)
    account:      PK=``user``            RK=``user_id``     -> credential + tokenVersion
    email lookup: PK=``email``           RK=``email_norm``  -> {userId}
libraries (``photolibraries``)
    PK=``library`` RK=``library_id`` -> {name, ownerUserId, createdAt}
memberships (``photomemberships``) — stored in both orientations:
    by library:   PK=``lib:``+library_id RK=``user_id``     -> {isOwner, joinedAt}
    by user:      PK=``usr:``+user_id     RK=``library_id``  -> {isOwner, joinedAt}
invites (``photoinvites``)
    invite:       PK=``library_id``       RK=``invite_id``   -> {emailNorm, tokenHash, ...}
    token lookup: PK=``token``            RK=``token_hash``  -> {inviteId, libraryId}
    send throttle:PK=``throttle``         RK=``library_id``  -> {lastSentAt, window...}
audit (``photoaudit``)
    PK=``library_id`` RK=``{iso_ts}-{uuid}`` -> {actor, action, target, at}
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

# The cap on how many members a single library may have, counting the owner and
# any outstanding (pending) invites. Settled with the product owner.
MAX_LIBRARY_MEMBERS = 15

# Invited accounts / memberships expire if the link is not used within 72h.
INVITE_TTL_SECONDS = 72 * 3600

# Invite-send throttle (mirrors password_auth.reset_email_allowed): a minimum
# interval between sends plus a rolling hourly cap, per library.
_INVITE_MIN_INTERVAL_SECONDS = 30
_INVITE_MAX_PER_HOUR = 30

# Partition-key namespaces / well-known keys.
_USER_PK = 'user'
_EMAIL_PK = 'email'
_LIBRARY_PK = 'library'
_MEMBER_LIB_PREFIX = 'lib:'
_MEMBER_USR_PREFIX = 'usr:'
_TOKEN_PK = 'token'
_THROTTLE_PK = 'throttle'
# Per-user password reset (stored in the users table).
_RESET_PK = 'reset'
_RESET_THROTTLE_PK = 'reset-throttle'


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def normalize_email(email: Optional[str]) -> str:
    return str(email or '').strip().lower()


def new_user_id() -> str:
    """Opaque, collision-resistant id for a freshly created account."""
    return 'u' + uuid.uuid4().hex


def new_invite_id() -> str:
    return 'inv' + uuid.uuid4().hex


def generate_invite_token() -> str:
    return secrets.token_urlsafe(32)


def hash_invite_token(raw: str) -> str:
    return hashlib.sha256((raw or '').encode('utf-8')).hexdigest()


def _now() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes')


class LibraryStore:
    """Reads/writes the account, library, membership, invite, and audit tables."""

    def __init__(
        self,
        *,
        users_table,
        libraries_table,
        memberships_table,
        invites_table,
        audit_table,
    ) -> None:
        self.users = users_table
        self.libraries = libraries_table
        self.memberships = memberships_table
        self.invites = invites_table
        self.audit_table = audit_table

    # -- generic table helpers ------------------------------------------------
    @staticmethod
    def _get(table, pk: str, rk: str) -> Optional[Dict]:
        try:
            return dict(table.get_entity(partition_key=pk, row_key=rk))
        except Exception:
            return None

    @staticmethod
    def _query(table, filter_str: str) -> List[Dict]:
        try:
            return [dict(row) for row in table.query_entities(filter_str)]
        except Exception:
            return []

    @staticmethod
    def _delete(table, pk: str, rk: str) -> None:
        try:
            table.delete_entity(partition_key=pk, row_key=rk)
        except Exception:
            pass

    # -- accounts -------------------------------------------------------------
    def get_user(self, user_id: str) -> Optional[Dict]:
        if not user_id:
            return None
        return self._get(self.users, _USER_PK, str(user_id))

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        norm = normalize_email(email)
        if not norm:
            return None
        lookup = self._get(self.users, _EMAIL_PK, norm)
        if not lookup:
            return None
        return self.get_user(str(lookup.get('userId') or ''))

    def user_exists_for_email(self, email: str) -> bool:
        return self.get_user_by_email(email) is not None

    def create_user(
        self,
        *,
        email: str,
        password_hash: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict:
        """Create an account + its email lookup row. Idempotent per user_id.

        The account's personal library id equals its user id. Callers are
        responsible for also creating the library + owner membership (see
        :meth:`ensure_personal_library`).
        """
        uid = str(user_id or new_user_id())
        norm = normalize_email(email)
        now = _now()
        entity = {
            'PartitionKey': _USER_PK,
            'RowKey': uid,
            'email': email or '',
            'emailNorm': norm,
            'tokenVersion': 1,
            'personalLibraryId': uid,
            'createdAt': now,
        }
        if password_hash:
            entity['passwordHash'] = password_hash
        self.users.upsert_entity(entity)
        if norm:
            self.users.upsert_entity({
                'PartitionKey': _EMAIL_PK,
                'RowKey': norm,
                'userId': uid,
            })
        return entity

    def set_user_email(self, user_id: str, email: str) -> None:
        """Update an account's email and rewrite its email->id lookup row.

        Lets an operator recover login-by-email for an owner that was seeded
        without an email (e.g. OWNER_EMAIL set after the fact)."""
        account = self.get_user(user_id)
        if account is None:
            return
        old_norm = normalize_email(account.get('emailNorm') or account.get('email'))
        new_norm = normalize_email(email)
        account['email'] = email or ''
        account['emailNorm'] = new_norm
        account['PartitionKey'] = _USER_PK
        account['RowKey'] = str(user_id)
        self.users.upsert_entity(account)
        if old_norm and old_norm != new_norm:
            self._delete(self.users, _EMAIL_PK, old_norm)
        if new_norm:
            self.users.upsert_entity({
                'PartitionKey': _EMAIL_PK,
                'RowKey': new_norm,
                'userId': str(user_id),
            })

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        entity = self.get_user(user_id)
        if not entity:
            return
        entity['passwordHash'] = password_hash
        entity['RowKey'] = str(user_id)
        entity['PartitionKey'] = _USER_PK
        self.users.upsert_entity(entity)

    def delete_user(self, user_id: str) -> None:
        """Delete an account row and its email lookup (account deletion)."""
        account = self.get_user(user_id)
        if account is not None:
            norm = normalize_email(account.get('emailNorm') or account.get('email'))
            if norm:
                self._delete(self.users, _EMAIL_PK, norm)
        self._delete(self.users, _USER_PK, str(user_id))

    def token_version(self, user_id: str) -> int:
        entity = self.get_user(user_id)
        if not entity:
            return 0
        try:
            return int(entity.get('tokenVersion', 1) or 1)
        except (TypeError, ValueError):
            return 1

    def bump_token_version(self, user_id: str) -> int:
        """Invalidate all of a user's existing session tokens. Returns new ver."""
        entity = self.get_user(user_id)
        if not entity:
            return 0
        current = self.token_version(user_id)
        entity['tokenVersion'] = current + 1
        entity['PartitionKey'] = _USER_PK
        entity['RowKey'] = str(user_id)
        self.users.upsert_entity(entity)
        return current + 1

    # -- password reset (per user, single-use, hashed at rest) ----------------
    def create_reset_token(self, user_id: str, *, ttl_seconds: int = 3600) -> str:
        raw = secrets.token_urlsafe(32)
        self.users.upsert_entity({
            'PartitionKey': _RESET_PK,
            'RowKey': hash_invite_token(raw),
            'userId': str(user_id),
            'expiresAt': _now() + int(ttl_seconds),
        })
        return raw

    def consume_reset_token(self, raw: str) -> Optional[str]:
        """Validate + invalidate a reset token. Returns the user_id or None."""
        if not raw:
            return None
        token_hash = hash_invite_token(raw)
        entity = self._get(self.users, _RESET_PK, token_hash)
        # Always delete on any consume attempt so a token can't be brute-forced.
        self._delete(self.users, _RESET_PK, token_hash)
        if not entity:
            return None
        if int(entity.get('expiresAt', 0) or 0) <= _now():
            return None
        return str(entity.get('userId') or '') or None

    def reset_email_allowed(
        self,
        user_id: str,
        *,
        min_interval_seconds: int = 60,
        max_per_hour: int = 5,
    ) -> bool:
        """Throttle reset emails per user (records the send when allowed)."""
        now = _now()
        entity = self._get(self.users, _RESET_THROTTLE_PK, str(user_id)) or {
            'PartitionKey': _RESET_THROTTLE_PK,
            'RowKey': str(user_id),
        }
        last_sent = int(entity.get('lastSentAt', 0) or 0)
        window_start = int(entity.get('windowStart', 0) or 0)
        window_count = int(entity.get('windowCount', 0) or 0)
        if now - last_sent < int(min_interval_seconds):
            return False
        if now - window_start >= 3600:
            window_start = now
            window_count = 0
        if window_count >= int(max_per_hour):
            return False
        entity['lastSentAt'] = now
        entity['windowStart'] = window_start
        entity['windowCount'] = window_count + 1
        try:
            self.users.upsert_entity(entity)
        except Exception:
            return False
        return True

    # -- libraries ------------------------------------------------------------
    def get_library(self, library_id: str) -> Optional[Dict]:
        if not library_id:
            return None
        return self._get(self.libraries, _LIBRARY_PK, str(library_id))

    def create_library(self, library_id: str, *, name: str, owner_user_id: str) -> Dict:
        entity = {
            'PartitionKey': _LIBRARY_PK,
            'RowKey': str(library_id),
            'name': name or '',
            'ownerUserId': str(owner_user_id),
            'createdAt': _now(),
        }
        self.libraries.upsert_entity(entity)
        return entity

    def rename_library(self, library_id: str, name: str) -> bool:
        entity = self.get_library(library_id)
        if not entity:
            return False
        entity['name'] = name or ''
        entity['PartitionKey'] = _LIBRARY_PK
        entity['RowKey'] = str(library_id)
        self.libraries.upsert_entity(entity)
        return True

    def library_owner_id(self, library_id: str) -> str:
        entity = self.get_library(library_id)
        return str((entity or {}).get('ownerUserId') or '')

    def delete_library(self, library_id: str) -> None:
        self._delete(self.libraries, _LIBRARY_PK, str(library_id))

    # -- memberships ----------------------------------------------------------
    def get_membership(self, user_id: str, library_id: str) -> Optional[Dict]:
        if not user_id or not library_id:
            return None
        return self._get(self.memberships, _MEMBER_LIB_PREFIX + str(library_id), str(user_id))

    def is_member(self, user_id: str, library_id: str) -> bool:
        return self.get_membership(user_id, library_id) is not None

    def is_member_email(self, email: str, library_id: str) -> bool:
        account = self.get_user_by_email(email)
        if not account:
            return False
        return self.is_member(str(account.get('RowKey') or ''), library_id)

    def is_owner(self, user_id: str, library_id: str) -> bool:
        membership = self.get_membership(user_id, library_id)
        return bool(membership and _as_bool(membership.get('isOwner')))

    def add_membership(self, user_id: str, library_id: str, *, is_owner: bool = False) -> None:
        joined_at = _now()
        self.memberships.upsert_entity({
            'PartitionKey': _MEMBER_LIB_PREFIX + str(library_id),
            'RowKey': str(user_id),
            'isOwner': bool(is_owner),
            'joinedAt': joined_at,
        })
        self.memberships.upsert_entity({
            'PartitionKey': _MEMBER_USR_PREFIX + str(user_id),
            'RowKey': str(library_id),
            'isOwner': bool(is_owner),
            'joinedAt': joined_at,
        })

    def remove_membership(self, user_id: str, library_id: str) -> None:
        self._delete(self.memberships, _MEMBER_LIB_PREFIX + str(library_id), str(user_id))
        self._delete(self.memberships, _MEMBER_USR_PREFIX + str(user_id), str(library_id))

    def list_library_members(self, library_id: str) -> List[Dict]:
        rows = self._query(self.memberships, f"PartitionKey eq '{_MEMBER_LIB_PREFIX}{library_id}'")
        members = []
        for row in rows:
            members.append({
                'userId': str(row.get('RowKey') or ''),
                'isOwner': _as_bool(row.get('isOwner')),
                'joinedAt': int(row.get('joinedAt', 0) or 0),
            })
        return members

    def member_count(self, library_id: str) -> int:
        return len(self.list_library_members(library_id))

    def list_user_libraries(self, user_id: str) -> List[Dict]:
        """Libraries this user belongs to, joined with library metadata."""
        rows = self._query(self.memberships, f"PartitionKey eq '{_MEMBER_USR_PREFIX}{user_id}'")
        libraries = []
        for row in rows:
            library_id = str(row.get('RowKey') or '')
            meta = self.get_library(library_id) or {}
            libraries.append({
                'libraryId': library_id,
                'name': str(meta.get('name') or ''),
                'ownerUserId': str(meta.get('ownerUserId') or ''),
                'isOwner': _as_bool(row.get('isOwner')),
            })
        return libraries

    def ensure_personal_library(self, user_id: str, *, name: str = '') -> str:
        """Idempotently create a user's own library + owner membership."""
        library_id = str(user_id)
        if not self.get_library(library_id):
            self.create_library(library_id, name=name, owner_user_id=user_id)
        if not self.get_membership(user_id, library_id):
            self.add_membership(user_id, library_id, is_owner=True)
        return library_id

    # -- invites --------------------------------------------------------------
    def pending_invites(self, library_id: str, *, target_type: Optional[str] = None) -> List[Dict]:
        now = _now()
        rows = self._query(self.invites, f"PartitionKey eq '{library_id}'")
        pending = []
        for row in rows:
            if str(row.get('status') or '') != 'pending':
                continue
            if int(row.get('expiresAt', 0) or 0) <= now:
                continue
            if target_type is not None and str(row.get('targetType') or '') != target_type:
                continue
            pending.append(row)
        return pending

    def pending_invite_count(self, library_id: str, *, target_type: Optional[str] = None) -> int:
        return len(self.pending_invites(library_id, target_type=target_type))

    def effective_member_count(self, library_id: str) -> int:
        """Accepted members + outstanding pending *join* invites (both hold a
        slot). "Fresh" invites create the invitee's own library, so they do not
        consume this library's capacity."""
        return self.member_count(library_id) + self.pending_invite_count(library_id, target_type='join')

    def has_capacity(self, library_id: str) -> bool:
        return self.effective_member_count(library_id) < MAX_LIBRARY_MEMBERS

    def find_pending_invite_for_email(self, library_id: str, email: str) -> Optional[Dict]:
        norm = normalize_email(email)
        for row in self.pending_invites(library_id):
            if normalize_email(row.get('emailNorm')) == norm:
                return row
        return None

    def create_invite(
        self,
        *,
        library_id: str,
        email: str,
        target_type: str,
        invited_by: str,
        ttl_seconds: int = INVITE_TTL_SECONDS,
    ) -> str:
        """Create a pending, email-bound, single-use invite. Returns raw token."""
        raw = generate_invite_token()
        token_hash = hash_invite_token(raw)
        invite_id = new_invite_id()
        norm = normalize_email(email)
        expires_at = _now() + int(ttl_seconds)
        self.invites.upsert_entity({
            'PartitionKey': str(library_id),
            'RowKey': invite_id,
            'emailNorm': norm,
            'tokenHash': token_hash,
            'targetType': 'join' if target_type == 'join' else 'fresh',
            'status': 'pending',
            'invitedBy': str(invited_by),
            'createdAt': _now(),
            'expiresAt': expires_at,
        })
        self.invites.upsert_entity({
            'PartitionKey': _TOKEN_PK,
            'RowKey': token_hash,
            'inviteId': invite_id,
            'libraryId': str(library_id),
        })
        return raw

    def get_invite_by_token(self, raw_token: str) -> Optional[Dict]:
        """Resolve a raw invite token to its invite row if valid & pending."""
        if not raw_token:
            return None
        token_hash = hash_invite_token(raw_token)
        lookup = self._get(self.invites, _TOKEN_PK, token_hash)
        if not lookup:
            return None
        invite = self._get(self.invites, str(lookup.get('libraryId') or ''), str(lookup.get('inviteId') or ''))
        if not invite:
            return None
        if str(invite.get('status') or '') != 'pending':
            return None
        if int(invite.get('expiresAt', 0) or 0) <= _now():
            return None
        if not hmac.compare_digest(str(invite.get('tokenHash') or ''), token_hash):
            return None
        return invite

    def mark_invite_accepted(self, invite: Dict) -> None:
        invite = dict(invite)
        invite['status'] = 'accepted'
        invite['acceptedAt'] = _now()
        self.invites.upsert_entity(invite)
        self._delete(self.invites, _TOKEN_PK, str(invite.get('tokenHash') or ''))

    def delete_all_invites(self, library_id: str) -> None:
        """Delete every invite (and its token lookup) for a library."""
        for row in self._query(self.invites, f"PartitionKey eq '{library_id}'"):
            self._delete(self.invites, _TOKEN_PK, str(row.get('tokenHash') or ''))
            self._delete(self.invites, str(library_id), str(row.get('RowKey') or ''))

    def delete_all_memberships(self, library_id: str) -> None:
        """Delete every membership row (both orientations) for a library."""
        for m in self.list_library_members(library_id):
            self.remove_membership(m['userId'], library_id)

    def revoke_invite(self, library_id: str, invite_id: str) -> bool:
        invite = self._get(self.invites, str(library_id), str(invite_id))
        if not invite or str(invite.get('status') or '') != 'pending':
            return False
        invite['status'] = 'revoked'
        self.invites.upsert_entity(invite)
        self._delete(self.invites, _TOKEN_PK, str(invite.get('tokenHash') or ''))
        return True

    def invite_send_allowed(self, library_id: str) -> bool:
        """Rate-limit invite emails per library. Records the send when allowed.

        Mirrors password_auth.reset_email_allowed: fails closed if the throttle
        row cannot be written, so a storage error never enables a flood.
        """
        now = _now()
        entity = self._get(self.invites, _THROTTLE_PK, str(library_id)) or {
            'PartitionKey': _THROTTLE_PK,
            'RowKey': str(library_id),
        }
        last_sent = int(entity.get('lastSentAt', 0) or 0)
        window_start = int(entity.get('windowStart', 0) or 0)
        window_count = int(entity.get('windowCount', 0) or 0)
        if now - last_sent < _INVITE_MIN_INTERVAL_SECONDS:
            return False
        if now - window_start >= 3600:
            window_start = now
            window_count = 0
        if window_count >= _INVITE_MAX_PER_HOUR:
            return False
        entity['lastSentAt'] = now
        entity['windowStart'] = window_start
        entity['windowCount'] = window_count + 1
        try:
            self.invites.upsert_entity(entity)
        except Exception:
            return False
        return True

    # -- audit ----------------------------------------------------------------
    def audit(self, library_id: str, *, actor: str, action: str, target: str = '') -> None:
        try:
            self.audit_table.upsert_entity({
                'PartitionKey': str(library_id),
                'RowKey': f'{_iso_now()}-{uuid.uuid4().hex}',
                'actor': str(actor),
                'action': str(action),
                'target': str(target),
                'at': _iso_now(),
            })
        except Exception:
            # Audit is best-effort; never fail the operation because logging failed.
            pass
