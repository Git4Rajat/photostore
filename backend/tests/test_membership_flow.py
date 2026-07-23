"""End-to-end style tests of the membership flow the library endpoints drive.

The Flask endpoints can't be imported without the full Azure/vision stack, so
these exercise the LibraryStore sequence each endpoint performs, asserting the
security-critical invariants: isolation, immediate revocation, and the cap.
"""
import library_utils
from fakes import make_store


def _seed_owner(s, uid='owner', email='owner@x.com', name='Owner Lib'):
    s.create_user(email=email, user_id=uid, password_hash='h')
    s.ensure_personal_library(uid, name=name)
    return uid


def test_invite_accept_new_account_join_flow():
    s = make_store()
    owner = _seed_owner(s)

    # library_invite: owner invites bob to join
    assert s.has_capacity(owner)
    raw = s.create_invite(library_id=owner, email='bob@x.com', target_type='join', invited_by=owner)
    assert s.effective_member_count(owner) == 2  # owner + pending

    # library_invite/accept for a NEW account (password branch)
    invite = s.get_invite_by_token(raw)
    assert invite is not None
    bob = library_utils.new_user_id()
    s.create_user(email='bob@x.com', user_id=bob, password_hash='h')
    s.ensure_personal_library(bob, name='bob@x.com')
    s.add_membership(bob, owner, is_owner=False)
    s.mark_invite_accepted(invite)

    # bob now belongs to BOTH his own and the owner's library
    assert {l['libraryId'] for l in s.list_user_libraries(bob)} == {bob, owner}
    assert s.member_count(owner) == 2
    assert s.effective_member_count(owner) == 2  # pending converted to member


def test_isolation_non_member_cannot_access():
    s = make_store()
    owner = _seed_owner(s)
    carol = _seed_owner(s, uid='ucarol', email='carol@x.com', name='Carol Lib')
    # carol is NOT a member of owner's library -> resolver would 403
    assert s.is_member(carol, owner) is False
    # switching to a library you don't belong to is rejected
    assert s.is_member(carol, owner) is False


def test_owner_removal_is_immediate():
    s = make_store()
    owner = _seed_owner(s)
    bob = _seed_owner(s, uid='ubob', email='bob@x.com', name='Bob Lib')
    s.add_membership(bob, owner, is_owner=False)
    assert s.is_member(bob, owner) is True

    # library_members/remove
    s.remove_membership(bob, owner)
    # The per-request membership check now fails for bob in owner's library...
    assert s.is_member(bob, owner) is False
    # ...but bob keeps access to his OWN library (removal is not account-wide).
    assert s.is_member(bob, bob) is True


def test_capacity_blocks_16th_join_invite():
    s = make_store()
    owner = _seed_owner(s)
    # 14 accepted members -> 15 total, at cap
    for i in range(library_utils.MAX_LIBRARY_MEMBERS - 1):
        s.add_membership(f'm{i}', owner)
    assert s.has_capacity(owner) is False
    # a fresh invite does NOT consume this library's capacity
    s.create_invite(library_id=owner, email='fresh@x.com', target_type='fresh', invited_by=owner)
    assert s.has_capacity(owner) is False  # unchanged; fresh doesn't reserve a seat here


def test_accept_can_fill_the_final_seat():
    # Regression: the accept gate must count accepted members only, not the
    # invitee's own pending seat, or the member filling the last slot is rejected.
    s = make_store()
    owner = _seed_owner(s)
    # owner + 13 members = 14 accepted; one pending join invite reserves the 15th.
    for i in range(library_utils.MAX_LIBRARY_MEMBERS - 2):
        s.add_membership(f'm{i}', owner)
    assert s.member_count(owner) == library_utils.MAX_LIBRARY_MEMBERS - 1
    raw = s.create_invite(library_id=owner, email='last@x.com', target_type='join', invited_by=owner)
    # has_capacity would be False here (members + pending == MAX), which is the bug.
    assert s.has_capacity(owner) is False
    # The endpoint's gate is member_count < MAX, which still admits this invitee.
    invite = s.get_invite_by_token(raw)
    assert s.member_count(owner) < library_utils.MAX_LIBRARY_MEMBERS
    s.add_membership('mlast', owner, is_owner=False)
    s.mark_invite_accepted(invite)
    assert s.member_count(owner) == library_utils.MAX_LIBRARY_MEMBERS
    # And now the library is genuinely full for the next member.
    assert s.member_count(owner) >= library_utils.MAX_LIBRARY_MEMBERS


def test_self_leave_returns_to_own_library():
    s = make_store()
    owner = _seed_owner(s)
    bob = _seed_owner(s, uid='ubob', email='bob@x.com', name='Bob Lib')
    s.add_membership(bob, owner, is_owner=False)

    # library_leave: bob leaves owner's library (not his own, not one he owns)
    assert s.library_owner_id(owner) != bob
    s.remove_membership(bob, owner)
    assert {l['libraryId'] for l in s.list_user_libraries(bob)} == {bob}


def test_delete_library_tears_down_account_and_sharing():
    s = make_store()
    bob = _seed_owner(s, uid='ubob', email='bob@x.com', name='Bob Lib')
    s.create_invite(library_id=bob, email='x@x.com', target_type='join', invited_by=bob)

    # library_delete (no other members): teardown
    s.delete_all_invites(bob)
    s.delete_all_memberships(bob)
    s.delete_library(bob)
    s.delete_user(bob)

    assert s.get_user(bob) is None
    assert s.get_user_by_email('bob@x.com') is None
    assert s.get_library(bob) is None
    assert s.list_user_libraries(bob) == []
    assert s.pending_invite_count(bob) == 0
