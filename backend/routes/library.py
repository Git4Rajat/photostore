"""Blueprint: library routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

library_bp = Blueprint('library', __name__)

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

@library_bp.route('/api/library/invite/info', methods=['GET'])
def library_invite_info():
    """Public: minimal, non-sensitive details so the accept page can render.

    Reveals only what the recipient already knows (they hold the emailed link):
    the target library name and whether they must set a password (new account).
    """
    token = str(app.request.args.get('token', '') or '')
    invite = app.library_store.get_invite_by_token(token) if app.library_store else None
    if not invite:
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
    data = app.request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    invite = app.library_store.get_invite_by_token(token) if app.library_store else None
    if not invite:
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
        account_id = app.library_utils.new_user_id()
        app.library_store.create_user(email=email, password_hash=app.password_auth.hash_password(password), user_id=account_id)
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
    email = str((app.library_store.get_user(account_id) or {}).get('email') or '')
    token = app._issue_session_for(account_id, library_id=account_id, email=email, mode=app.AUTH_MODE)
    return app.jsonify({'status': 'ok', 'token': token, 'activeLibraryId': account_id})

@library_bp.route('/api/library', methods=['DELETE'])
def library_delete():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    # The primary owner is the deployment root (recreated from the deploy seed),
    # so it can't self-delete; invited users may delete their own library.
    if library_id == app.password_auth.OWNER_USER_ID:
        return app.jsonify({'error': 'The primary owner account cannot be deleted.'}), 400
    others = [m for m in app.library_store.list_library_members(library_id) if m['userId'] != account_id]
    if others:
        return app.jsonify({'error': 'Remove all other members before deleting this library.'}), 409

    app.library_store.audit(library_id, actor=account_id, action='delete-library', target=library_id)
    app.library_store.delete_all_invites(library_id)
    app.library_store.delete_all_memberships(library_id)
    app.library_store.delete_library(library_id)
    # Account deletion. The resolver rejects tokens whose account row is gone
    # (rather than re-creating it), so the caller's active session stops working
    # on its next request.
    app.library_store.delete_user(account_id)
    app._enqueue_library_purge_job(library_id)
    return app.jsonify({'status': 'ok'})

@library_bp.route('/api/library/download/request', methods=['POST'])
def library_download_request():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    existing_job_id = app._has_active_library_download_job(library_id)
    if existing_job_id:
        return app.jsonify(app._clustering_queue_response({'status': 'queued', 'jobId': existing_job_id}, activeLibraryId=library_id))
    meta = app.library_store.get_library(library_id) or {}
    library_name = str(meta.get('name') or '')
    queued = app._enqueue_library_download_job(library_id, account_id, library_name)
    return app.jsonify(app._clustering_queue_response(queued, activeLibraryId=library_id))

@library_bp.route('/api/library/download/status', methods=['GET'])
def library_download_status():
    _account_id, _library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(app.request.args.get('jobId', '') or '')
    if not job_id or app.metadata_table_client is None:
        return app.jsonify({'status': 'unknown'})
    try:
        row = app.metadata_table_client.get_entity(partition_key='jobs', row_key=app._job_row_key(job_id))
    except Exception:
        return app.jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = app.json.loads(result)
        except Exception:
            pass
    return app.jsonify({'status': str(row.get('status') or 'unknown'), 'result': result, 'error': row.get('error')})

@library_bp.route('/api/library/export/manifest', methods=['GET'])
def library_export_manifest_page():
    """One page of {filename, blobName} entries covering every non-deleted
    photo/video in the caller's library, plus a single container-scoped
    baseUrl/sas shared by every file on every page -- the client builds each
    file's actual URL itself as f'{baseUrl}/{encodeURIComponent(blobName)}?
    {sas}'. One signature per request instead of one per file: at up to
    ~500k rows that's the difference between the backend doing O(page size)
    or O(library size) work per page (confirmed live 2026-09-05 after a user
    asked for exactly this -- generate_container_sas is a pure local HMAC
    computation, so per-file signing wasn't slow, just needless work repeated
    thousands of times over a large export). The final download filename is
    restored client-side (see libraryExportDownloader.ts's triggerBrowserSave,
    which sets the <a download> attribute explicitly) rather than via a
    per-blob Content-Disposition override, since a shared SAS can't carry a
    per-blob response header anyway.

    Paginated via the Table service's own continuation token (opaque to the
    caller, base64'd for safe transport as a query param) rather than
    materializing the whole library per request -- at up to ~500k rows, the
    client calls this repeatedly as it works through its download queue, so
    each page fetch needs to cost O(page size), not O(library size).
    """
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    if app.metadata_table_client is None:
        # An empty-but-shaped-like-success {files: [], nextCursor: None}
        # response here would satisfy the client's type but not its actual
        # contract (baseUrl/sas are also required, see ExportManifestPage in
        # libraryClient.ts) -- the export would silently treat a backend
        # storage outage as "this library has zero files" and report "Done"
        # rather than surfacing the real problem, since a 200 response never
        # makes getLibraryExportManifestPage throw. Matching the same-shaped
        # failure a few lines below (SAS signing failing) instead.
        return app.jsonify({'error': 'Could not list library contents.'}), 503

    try:
        page_size = max(1, min(int(app.request.args.get('pageSize', str(app.LIBRARY_EXPORT_MANIFEST_PAGE_SIZE))), 2000))
    except ValueError:
        return app.jsonify({'error': 'Invalid pageSize.'}), 400

    raw_cursor = str(app.request.args.get('cursor', '') or '')
    continuation_token = app._decode_export_manifest_cursor(raw_cursor) if raw_cursor else None
    if raw_cursor and continuation_token is None:
        return app.jsonify({'error': 'Invalid cursor.'}), 400

    try:
        base_url, sas, _expires_at = app._stable_container_read_sas(app.BLOB_IMAGE_CONTAINER)
    except Exception:
        app.app.logger.exception('Failed to sign library export manifest for %s', library_id)
        return app.jsonify({'error': 'Could not prepare download links.'}), 503

    pk = app._escape_odata(library_id)
    pager = None
    try:
        pager = app.metadata_table_client.query_entities(
            f"PartitionKey eq '{pk}'",
            select=['PartitionKey', 'RowKey', 'processing_state'],
            results_per_page=page_size,
        ).by_page(continuation_token=continuation_token)
        rows = list(next(pager))
    except StopIteration:
        rows = []
    except Exception:
        app.app.logger.exception('Failed to page library export manifest for %s', library_id)
        return app.jsonify({'error': 'Could not list library contents.'}), 503

    next_cursor = app._encode_export_manifest_cursor(pager.continuation_token) if pager is not None else None

    files = []
    for row in rows:
        filename = str(row.get('RowKey') or '')
        if not filename or str(row.get('processing_state') or '').strip().lower() == 'deleted':
            continue
        try:
            blob_name = app.resolve_physical_blob_name(library_id, filename, 'image')
        except Exception:
            app.app.logger.warning('Skipping %s from export manifest for %s: could not resolve its blob', filename, library_id)
            continue
        files.append({'filename': filename, 'blobName': blob_name})

    return app.jsonify({'baseUrl': base_url, 'sas': sas, 'files': files, 'nextCursor': next_cursor})

@library_bp.route('/api/library/clean/request', methods=['POST'])
def library_clean_request():
    account_id, library_id, error = app._require_owner_context()
    if error:
        return error
    if not app.email_utils.is_configured():
        return app.jsonify({'error': 'Email delivery is not configured, so this action is unavailable.'}), 503
    if app.library_store.get_active_clean_request(library_id):
        return app.jsonify({'error': 'A cleanup confirmation is already pending for this library.'}), 409

    data = app.request.get_json(silent=True) or {}
    if app.AUTH_MODE == 'password':
        password = str(data.get('password', '') or '')
        account = app.library_store.get_user(account_id) or {}
        stored_hash = str(account.get('passwordHash') or '')
        if not stored_hash or not app.password_auth.verify_password(password, stored_hash):
            return app.jsonify({'error': 'Incorrect password.'}), 401

    if not app.library_store.clean_request_send_allowed(library_id):
        return app.jsonify({'error': 'Too many attempts recently. Please wait and try again.'}), 429

    other_member_ids = [m['userId'] for m in app.library_store.list_library_members(library_id) if m['userId'] != account_id]
    required_user_ids = [account_id]
    if other_member_ids:
        required_user_ids.append(app.random.choice(other_member_ids))

    request_id, tokens_by_user = app.library_store.create_clean_request(
        library_id=library_id, requested_by=account_id, required_user_ids=required_user_ids,
        ttl_seconds=app.library_utils.CLEAN_REQUEST_TTL_SECONDS,
    )
    meta = app.library_store.get_library(library_id) or {}
    requester_email = str((app.library_store.get_user(account_id) or {}).get('email') or '')
    base = (app.PUBLIC_APP_BASE_URL or '').rstrip('/')
    sent_to = []
    for user_id, raw in tokens_by_user.items():
        recipient_email = str((app.library_store.get_user(user_id) or {}).get('email') or '')
        if not recipient_email:
            continue
        confirm_url = f'{base}/confirm-library-clean?token={raw}'
        try:
            app.email_utils.send_library_clean_email(
                recipient_email, confirm_url,
                library_name=str(meta.get('name') or ''),
                requested_by=requester_email,
            )
            sent_to.append(app.email_utils.masked_recipient(recipient_email))
        except Exception as exc:
            app.app.logger.warning('Library clean confirmation email failed for %s: %s', user_id, exc)

    if not sent_to:
        app.library_store.cancel_clean_request(library_id, request_id)
        return app.jsonify({
            'error': 'Could not send confirmation email(s). Please try again.',
        }), 502

    app.library_store.audit(library_id, actor=account_id, action='clean-requested', target=request_id)
    return app.jsonify({
        'status': 'pending',
        'requiresAdditionalApproval': len(other_member_ids) > 0,
        'sentTo': sent_to,
        'expiresIn': app.library_utils.CLEAN_REQUEST_TTL_SECONDS,
    })

@library_bp.route('/api/library/clean/confirm', methods=['POST'])
def library_clean_confirm():
    account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    status, confirmed_request = app.library_store.confirm_clean_token(token, account_id=account_id, library_id=library_id)
    if status == 'mismatch':
        return app.jsonify({'error': 'This confirmation link does not belong to your account.'}), 403
    if status != 'ok' or not confirmed_request:
        return app.jsonify({'error': 'This confirmation link is invalid or has expired.'}), 400

    request_id = str(confirmed_request.get('RowKey') or '')
    app.library_store.audit(library_id, actor=account_id, action='clean-confirmed', target=request_id)
    if not app.library_store.is_clean_request_fully_confirmed(confirmed_request):
        return app.jsonify({'status': 'awaiting_more_approvals'})

    queued = app._enqueue_library_clean_job(library_id, account_id, request_id)
    app.library_store.cancel_clean_request(library_id, request_id)
    app.library_store.audit(library_id, actor=account_id, action='clean-executed', target=queued.get('jobId', ''))
    return app.jsonify(app._clustering_queue_response(queued, activeLibraryId=library_id))

@library_bp.route('/api/library/clean/status', methods=['GET'])
def library_clean_status():
    _account_id, _library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(app.request.args.get('jobId', '') or '')
    if not job_id or app.metadata_table_client is None:
        return app.jsonify({'status': 'unknown'})
    try:
        row = app.metadata_table_client.get_entity(partition_key='jobs', row_key=app._job_row_key(job_id))
    except Exception:
        stale_reason = app._reconcile_stale_library_cleanup(_library_id, job_id=job_id)
        if stale_reason:
            return app.jsonify({'status': 'failed', 'error': stale_reason})
        return app.jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = app.json.loads(result)
        except Exception:
            pass
    status = str(row.get('status') or 'unknown')
    stale_reason = None
    if status in {'queued', 'running'}:
        stale_reason = app._reconcile_stale_library_cleanup(_library_id, job_id=job_id, job_row=row)
        if stale_reason:
            status = 'failed'
    if app.library_store is not None and status in {'done', 'failed'}:
        try:
            if status == 'done':
                summary = result if isinstance(result, dict) else {}
                app.library_store.set_cleanup_completed(
                    _library_id,
                    int(summary.get('photosDeleted') or 0),
                    int(summary.get('blobsDeleted') or 0),
                )
            else:
                app.library_store.set_cleanup_failed(_library_id, stale_reason or str(row.get('error') or 'cleanup failed'))
        except Exception:
            app.app.logger.debug('Could not reconcile cleanup status for %s from job %s', _library_id, job_id)
    return app.jsonify({'status': status, 'result': result, 'error': stale_reason or row.get('error')})

@library_bp.route('/api/library/cleanup-info', methods=['GET'])
@library_bp.route('/api/library/cleanup-info/', methods=['GET'])
def get_library_cleanup_info():
    """Get the cleanup status for the current library."""
    _account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    app._reconcile_stale_library_cleanup(library_id)
    meta = app.library_store.get_library(library_id) or {}
    return app.jsonify({
        'lastCleanupStatus': str(meta.get('lastCleanupStatus') or ''),
        'lastCleanupTime': int(meta.get('lastCleanupTime') or 0),
        'lastCleanupPhotosDeleted': int(meta.get('lastCleanupPhotosDeleted') or 0),
        'lastCleanupBlobsDeleted': int(meta.get('lastCleanupBlobsDeleted') or 0),
        'lastCleanupError': str(meta.get('lastCleanupError') or ''),
    })
