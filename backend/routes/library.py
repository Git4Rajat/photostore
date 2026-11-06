"""Core library management: membership, invitations, naming and switching.

Archive also registers this blueprint temporarily for older frontend bundles.
Whole-library operations live in library_operations.py. Shared helpers remain
in app.py with the existing late-binding semantics.
"""
from flask import Blueprint

import app

library_bp = Blueprint('library', __name__)


@library_bp.errorhandler(app.library_utils.InviteIndexNotReady)
def invitation_index_unavailable(_error):
    return app.jsonify({'error': 'Library invitations are temporarily unavailable. Please retry shortly.'}), 503


@library_bp.route('/api/library/mine', methods=['GET'])
def library_mine():
    """Libraries the caller belongs to (for the switcher) + the active one."""
    account_id, active_library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    if app.library_store is None:
        return app.jsonify({'activeLibraryId': active_library_id, 'libraries': []})
    return app.jsonify({
        'activeLibraryId': active_library_id,
        'libraries': app.library_store.list_user_libraries(account_id),
        'maxMembers': app.library_utils.MAX_LIBRARY_MEMBERS,
        'cleanupInfo': app.library_utils.cleanup_snapshot(app.library_store.get_library(active_library_id) or {}),
    })

@library_bp.route('/api/library/members', methods=['GET'])
def library_members():
    account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    if app.library_store is None:
        return app.jsonify({'members': []})
    meta = app.library_store.get_library(library_id) or {}
    is_owner = app.library_store.is_owner(account_id, library_id)
    body = {
        'libraryId': library_id,
        'name': str(meta.get('name') or ''),
        'ownerUserId': str(meta.get('ownerUserId') or ''),
        'isOwner': is_owner,
        'members': app._member_view(library_id, account_id),
        'maxMembers': app.library_utils.MAX_LIBRARY_MEMBERS,
        'cleanupInfo': app.library_utils.cleanup_snapshot(meta),
    }
    if is_owner:
        # Only the owner (who controls membership) sees outstanding invites.
        body['pendingInvites'] = [
            {
                'inviteId': str(inv.get('RowKey') or ''),
                'email': app.email_utils.masked_recipient(inv.get('emailNorm')),
                'targetType': str(inv.get('targetType') or ''),
                'expiresAt': int(inv.get('expiresAt', 0) or 0),
            }
            for inv in app.library_store.pending_invites(library_id)
        ]
    return app.jsonify(body)

@library_bp.route('/api/library/invite', methods=['POST'])
def library_invite():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    email = app.library_utils.normalize_email(data.get('email'))
    target_type = 'fresh' if str(data.get('targetType') or 'join') == 'fresh' else 'join'
    if not email or '@' not in email:
        return app.jsonify({'error': 'A valid email address is required.'}), 400
    # Invites are delivered only by email; without it the invitee would never get
    # the link, so refuse up front rather than reserving a seat for a dead invite.
    if not app.email_utils.is_configured():
        return app.jsonify({'error': 'Email delivery is not configured, so invitations cannot be sent.'}), 503
    if target_type == 'join' and not app.library_store.has_capacity(library_id):
        return app.jsonify({'error': f'This library is full (max {app.library_utils.MAX_LIBRARY_MEMBERS}).'}), 409
    if target_type == 'join' and app.library_store.is_member_email(email, library_id):
        return app.jsonify({'error': 'That person is already a member of this library.'}), 409
    if app.library_store.find_pending_invite_for_email(library_id, email):
        return app.jsonify({
            'error': 'An invitation was already sent to that email and is still pending. '
                     'Revoke it in the list below if you want to send a new one.',
        }), 409
    if not app.library_store.invite_send_allowed(library_id):
        return app.jsonify({'error': 'Too many invites sent recently. Please wait and try again.'}), 429

    raw = app.library_store.create_invite(
        library_id=library_id, email=email, target_type=target_type, invited_by=account_id,
    )
    meta = app.library_store.get_library(library_id) or {}
    inviter_email = str((app.library_store.get_user(account_id) or {}).get('email') or '')
    base = (app.PUBLIC_APP_BASE_URL or '').rstrip('/')
    invite_url = f'{base}/accept-invite?token={raw}'
    try:
        app.email_utils.send_invite_email(
            email, invite_url,
            library_name=str(meta.get('name') or '') if target_type == 'join' else '',
            inviter=inviter_email,
        )
    except Exception as exc:
        app.app.logger.warning('Invite email failed: %s', exc)
        # The link never went out; free the reserved seat rather than leave a
        # dangling pending invite the owner believes was delivered.
        invite = app.library_store.find_pending_invite_for_email(library_id, email)
        if invite:
            app.library_store.revoke_invite(library_id, str(invite.get('RowKey') or ''))
        return app.jsonify({
            'error': 'The invitation email could not be sent. Please try again.',
        }), 502
    app.library_store.audit(library_id, actor=account_id, action=f'invite:{target_type}', target=email)
    # Uniform response: never reveal whether the email already had an account.
    return app.jsonify({'status': 'sent'})

@library_bp.route('/api/library/invite/revoke', methods=['POST'])
def library_invite_revoke():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    invite_id = str(data.get('inviteId', '') or '').strip()
    if not invite_id:
        return app.jsonify({'error': 'inviteId is required.'}), 400
    if not app.library_store.revoke_invite(library_id, invite_id):
        return app.jsonify({'error': 'That invitation is no longer pending.'}), 404
    app.library_store.audit(library_id, actor=account_id, action='invite-revoked', target=invite_id)
    return app.jsonify({'status': 'ok'})

def _invite_probe_row() -> str:
    ip = app.request.remote_addr or ''
    return 'invite-throttle:ip:' + app.hashlib.sha256(ip.encode('utf-8')).hexdigest()[:16]


def _invite_probe_blocked():
    """429 once one address has made too many invalid-token lookups. These
    endpoints are unauthenticated, so bad guesses are throttled like logins."""
    if not app.password_auth.login_attempt_allowed(app.config_table_client, _invite_probe_row()):
        return app.jsonify({'error': 'Too many attempts. Please wait and try again.'}), 429
    return None


def _invite_probe_failed():
    app.password_auth.record_login_failure(app.config_table_client, row_key=_invite_probe_row(), free_attempts=10)


@library_bp.route('/api/library/invite/info', methods=['GET'])
def library_invite_info():
    """Public: minimal, non-sensitive details so the accept page can render.

    Reveals only what the recipient already knows (they hold the emailed link):
    the target library name and whether they must set a password (new account).
    """
    blocked = _invite_probe_blocked()
    if blocked:
        return blocked
    token = str(app.request.args.get('token', '') or '')
    invite = app.library_store.get_invite_by_token(token) if app.library_store else None
    if not invite:
        _invite_probe_failed()
        return app.jsonify({'valid': False}), 404
    library_id = str(invite.get('PartitionKey') or '')
    target_type = str(invite.get('targetType') or 'join')
    meta = app.library_store.get_library(library_id) or {}
    email = str(invite.get('emailNorm') or '')
    needs_password = not app.library_store.user_exists_for_email(email) and app.AUTH_MODE == 'password'
    return app.jsonify({
        'valid': True,
        'email': email,
        'targetType': target_type,
        'libraryName': str(meta.get('name') or '') if target_type == 'join' else '',
        'accountExists': app.library_store.user_exists_for_email(email),
        'needsPassword': needs_password,
    })

@library_bp.route('/api/library/invite/accept', methods=['POST'])
def library_invite_accept():
    blocked = _invite_probe_blocked()
    if blocked:
        return blocked
    data = app.request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    invite = app.library_store.get_invite_by_token(token) if app.library_store else None
    if not invite:
        _invite_probe_failed()
        return app.jsonify({'error': 'This invitation is invalid or has expired.'}), 400
    email = app.library_utils.normalize_email(invite.get('emailNorm'))
    target_type = str(invite.get('targetType') or 'join')
    library_id = str(invite.get('PartitionKey') or '')

    existing = app.library_store.get_user_by_email(email)
    if existing is not None:
        # Existing account: require the caller to be signed in AS that account
        # (email binding + explicit consent click). Works in both auth modes.
        account_id, _active, auth_error = app._require_library_context(require_auth=True)
        if auth_error:
            return app.jsonify({'error': f'Please sign in as {email} to accept this invitation.'}), 401
        if app.library_utils.normalize_email((app.library_store.get_user(account_id) or {}).get('email')) != email:
            return app.jsonify({'error': 'This invitation is for a different account.'}), 403
        new_account = False
    else:
        # New account: only self-service in password mode. In Entra mode the
        # invitee must first sign in with Microsoft (which creates the account),
        # then the existing-account branch above applies.
        if app.AUTH_MODE != 'password':
            return app.jsonify({'error': 'Please sign in first, then open this invitation link again.'}), 401
        password = str(data.get('password', '') or '')
        if len(password) < 8:
            return app.jsonify({'error': 'Please choose a password of at least 8 characters.'}), 400
        if len(password) > 256:
            return app.jsonify({'error': 'Please choose a password of at most 256 characters.'}), 400
        account_id = app.library_utils.new_user_id()
        try:
            app.library_store.create_user(email=email, password_hash=app.password_auth.hash_password(password), user_id=account_id, exclusive=True)
        except app.library_utils.EmailAlreadyRegistered:
            return app.jsonify({'error': 'An account for this email was just created. Please sign in and open the link again.'}), 409
        app.library_store.ensure_personal_library(account_id, name=email)
        new_account = True

    if target_type == 'join':
        if not app.library_store.is_member(account_id, library_id):
            # This invitee's own reserved (pending) seat is about to convert into
            # a membership, so gate on accepted members only — using has_capacity
            # here would double-count the pending invite and wrongly reject the
            # member that fills the final slot.
            if app.library_store.member_count(library_id) >= app.library_utils.MAX_LIBRARY_MEMBERS:
                return app.jsonify({'error': f'This library is now full (max {app.library_utils.MAX_LIBRARY_MEMBERS}).'}), 409
            app.library_store.add_membership(account_id, library_id, is_owner=False)
    app.library_store.mark_invite_accepted(invite)
    app.library_store.audit(library_id, actor=account_id, action='invite-accepted', target=email)

    active_library = library_id if target_type == 'join' else account_id
    # Persist it, not just this session's token -- otherwise the very next
    # plain login (no invite link involved) mints a fresh token defaulting
    # back to the account's own library, silently undoing the join.
    app.library_store.set_current_library(account_id, active_library)
    token_out = app._issue_session_for(account_id, library_id=active_library, email=email, mode=app.AUTH_MODE)
    return app.jsonify({
        'status': 'accepted',
        'token': token_out,
        'activeLibraryId': active_library,
        'newAccount': new_account,
        'expiresIn': app.SESSION_TTL_SECONDS,
    })

@library_bp.route('/api/library/switch', methods=['POST'])
def library_switch():
    account_id, _active, error = app._require_library_context(require_auth=True)
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    target = str(data.get('libraryId', '') or '').strip()
    if not target or not app.library_store.is_member(account_id, target):
        return app.jsonify({'error': 'You are not a member of that library.'}), 403
    app.library_store.set_current_library(account_id, target)
    email = str((app.library_store.get_user(account_id) or {}).get('email') or '')
    token = app._issue_session_for(account_id, library_id=target, email=email, mode=app.AUTH_MODE)
    return app.jsonify({'token': token, 'activeLibraryId': target, 'expiresIn': app.SESSION_TTL_SECONDS})

@library_bp.route('/api/library/rename', methods=['POST'])
def library_rename():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    name = str(data.get('name', '') or '').strip()[:100]
    app.library_store.rename_library(library_id, name)
    app.library_store.audit(library_id, actor=account_id, action='rename', target=name)
    return app.jsonify({'status': 'ok', 'name': name})

@library_bp.route('/api/library/members/remove', methods=['POST'])
def library_remove_member():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    target = str(data.get('userId', '') or '').strip()
    if not target:
        return app.jsonify({'error': 'userId is required.'}), 400
    if target == account_id:
        return app.jsonify({'error': "You can't remove yourself; delete the library instead."}), 400
    if not app.library_store.is_member(target, library_id):
        return app.jsonify({'error': 'That person is not a member of this library.'}), 404
    app.library_store.remove_membership(target, library_id)
    # No token-version bump needed: the per-request membership check makes the
    # removal take effect immediately for this library, without disturbing the
    # removed user's access to their *own* library.
    app.library_store.audit(library_id, actor=account_id, action='remove-member', target=target)
    return app.jsonify({'status': 'ok'})

@library_bp.route('/api/library/leave', methods=['POST'])
def library_leave():
    account_id, _active, error = app._require_library_context(require_auth=True)
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    target_library = str(data.get('libraryId', '') or '').strip()
    if not target_library:
        return app.jsonify({'error': 'libraryId is required.'}), 400
    if target_library == account_id or app.library_store.library_owner_id(target_library) == account_id:
        return app.jsonify({'error': "You can't leave a library you own."}), 400
    if not app.library_store.is_member(account_id, target_library):
        return app.jsonify({'error': 'You are not a member of that library.'}), 404
    app.library_store.remove_membership(account_id, target_library)
    app.library_store.audit(target_library, actor=account_id, action='leave', target=account_id)
    # Drop the caller back into their own library.
    app.library_store.set_current_library(account_id, account_id)
    email = str((app.library_store.get_user(account_id) or {}).get('email') or '')
    token = app._issue_session_for(account_id, library_id=account_id, email=email, mode=app.AUTH_MODE)
    return app.jsonify({'status': 'ok', 'token': token, 'activeLibraryId': account_id})
