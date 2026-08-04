"""Unit tests for library_utils.LibraryStore (multi-tenant membership/invites)."""
import library_utils
from fakes import make_store


def test_account_and_personal_library():
    s = make_store()
    owner = s.create_user(email='Owner@Example.com', password_hash='hash1', user_id='owner')
    assert owner['emailNorm'] == 'owner@example.com'
    assert s.get_user('owner')['personalLibraryId'] == 'owner'
    # login-by-email lookup is case-insensitive
    assert s.get_user_by_email('OWNER@example.com')['RowKey'] == 'owner'
    assert s.user_exists_for_email('nobody@example.com') is False

    assert s.ensure_personal_library('owner', name='Owner Lib') == 'owner'
    assert s.get_library('owner')['ownerUserId'] == 'owner'
    assert s.is_owner('owner', 'owner') is True
    assert s.member_count('owner') == 1
    # idempotent
    s.ensure_personal_library('owner')
    assert s.member_count('owner') == 1


def test_token_version_bump():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='h')
    assert s.token_version('owner') == 1
    assert s.bump_token_version('owner') == 2
    assert s.token_version('owner') == 2


def test_membership_dual_orientation_and_removal():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='h')
    s.ensure_personal_library('owner', name='Owner Lib')
    s.create_user(email='bob@x.com', user_id='ubob', password_hash='h')
    s.ensure_personal_library('ubob', name='Bob Lib')

    s.add_membership('ubob', 'owner', is_owner=False)
    assert s.is_member('ubob', 'owner') is True
    assert s.is_owner('ubob', 'owner') is False
    assert s.member_count('owner') == 2
    # switcher view lists both, with joined library metadata
    libs = {l['libraryId']: l for l in s.list_user_libraries('ubob')}
    assert set(libs) == {'ubob', 'owner'}
    assert libs['owner']['name'] == 'Owner Lib'
    assert libs['owner']['isOwner'] is False

    s.remove_membership('ubob', 'owner')
    assert s.is_member('ubob', 'owner') is False
    assert {l['libraryId'] for l in s.list_user_libraries('ubob')} == {'ubob'}
    assert s.member_count('owner') == 1


def test_invite_lifecycle_email_bound_single_use():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='h')
    s.ensure_personal_library('owner')

    raw = s.create_invite(library_id='owner', email='Carol@x.com', target_type='join', invited_by='owner')
    # pending invite reserves a slot
    assert s.pending_invite_count('owner') == 1
    assert s.effective_member_count('owner') == 2
    assert s.find_pending_invite_for_email('owner', 'CAROL@x.com') is not None

    invite = s.get_invite_by_token(raw)
    assert invite is not None and invite['emailNorm'] == 'carol@x.com'
    assert s.get_invite_by_token('bogus') is None

    s.mark_invite_accepted(invite)
    assert s.get_invite_by_token(raw) is None  # single use
    assert s.pending_invite_count('owner') == 0


def test_invite_expiry_and_revoke():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='h')
    s.ensure_personal_library('owner')

    expired = s.create_invite(library_id='owner', email='dan@x.com', target_type='fresh',
                              invited_by='owner', ttl_seconds=-1)
    assert s.get_invite_by_token(expired) is None
    assert s.pending_invite_count('owner') == 0

    raw = s.create_invite(library_id='owner', email='eve@x.com', target_type='join', invited_by='owner')
    inv = s.get_invite_by_token(raw)
    assert s.revoke_invite('owner', inv['RowKey']) is True
    assert s.get_invite_by_token(raw) is None
    assert s.pending_invite_count('owner') == 0


def test_capacity_cap_counts_members_and_pending():
    s = make_store()
    s.create_user(email='o@x.com', user_id='o', password_hash='h')
    s.ensure_personal_library('o')
    for i in range(library_utils.MAX_LIBRARY_MEMBERS - 1):
        s.add_membership(f'm{i}', 'o')
    assert s.member_count('o') == library_utils.MAX_LIBRARY_MEMBERS
    assert s.has_capacity('o') is False


def test_invite_send_throttle():
    s = make_store()
    s.create_user(email='o@x.com', user_id='o', password_hash='h')
    s.ensure_personal_library('o')
    assert s.invite_send_allowed('o') is True
    assert s.invite_send_allowed('o') is False  # min-interval blocks the next immediate send


def test_reset_token_single_use_and_expiry():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='h')

    raw = s.create_reset_token('owner', ttl_seconds=3600)
    assert s.consume_reset_token(raw) == 'owner'
    assert s.consume_reset_token(raw) is None  # single use
    assert s.consume_reset_token('bogus') is None

    expired = s.create_reset_token('owner', ttl_seconds=-1)
    assert s.consume_reset_token(expired) is None


def test_set_password_and_bump_version():
    s = make_store()
    s.create_user(email='o@x.com', user_id='owner', password_hash='old')
    s.set_user_password('owner', 'newhash')
    assert s.get_user('owner')['passwordHash'] == 'newhash'
    assert s.token_version('owner') == 1
    assert s.bump_token_version('owner') == 2
