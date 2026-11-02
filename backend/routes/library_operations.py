"""Archive-only whole-library operations. Heavy work remains on workers."""
from flask import Blueprint

import app

library_operations_bp = Blueprint('library_operations', __name__)

@library_operations_bp.route('/api/library', methods=['DELETE'])
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

@library_operations_bp.route('/api/library/download/request', methods=['POST'])
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

@library_operations_bp.route('/api/library/download/status', methods=['GET'])
def library_download_status():
    _account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(app.request.args.get('jobId', '') or '')
    if not job_id:
        return app.jsonify({'status': 'unknown'})
    row = app._get_job_row(library_id, job_id)
    if row is None:
        return app.jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = app.json.loads(result)
        except Exception:
            pass
    return app.jsonify({'status': str(row.get('status') or 'unknown'), 'result': result, 'error': row.get('error')})

@library_operations_bp.route('/api/library/export/manifest', methods=['GET'])
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

@library_operations_bp.route('/api/library/clean/request', methods=['POST'])
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

@library_operations_bp.route('/api/library/clean/confirm', methods=['POST'])
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

@library_operations_bp.route('/api/library/clean/status', methods=['GET'])
def library_clean_status():
    _account_id, _library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(app.request.args.get('jobId', '') or '')
    if not job_id:
        return app.jsonify({'status': 'unknown'})
    row = app._get_job_row(_library_id, job_id)
    if row is None:
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

@library_operations_bp.route('/api/library/cleanup-info', methods=['GET'])
@library_operations_bp.route('/api/library/cleanup-info/', methods=['GET'])
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
