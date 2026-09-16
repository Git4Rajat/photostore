"""Blueprint: upload routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

upload_bp = Blueprint('upload', __name__)

@upload_bp.route('/upload/init', methods=['POST'])
@upload_bp.route('/upload/init/', methods=['POST'])
@upload_bp.route('/api/upload/init', methods=['POST'])
@upload_bp.route('/api/upload/init/', methods=['POST'])
def init_upload():
    user_id, error = app._require_user_id()
    if error:
        return error
    blocked = app._library_cleanup_block_reason(user_id)
    if blocked:
        return app.jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0))
    expected_hash = (data.get('sha256') or '').strip()

    if not filename:
        return app.jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0:
        return app.jsonify({'error': 'Invalid totalSize'}), 400
    if total_size > app.MAX_UPLOAD_FILE_BYTES:
        return app.jsonify({'error': 'File exceeds upload limit'}), 413

    upload_id = app.secure_filename(str(data.get('uploadId') or '')) or str(app.uuid.uuid4())
    direct = bool(data.get('directToBlob'))
    is_fresh_upload = not data.get('uploadId')
    if is_fresh_upload:
        try:
            app._cleanup_failed_upload(user_id, filename)
        except Exception:
            pass

    # Reserve the anonymous blob name the browser will upload to. Persisted on the
    # metadata row (durable + replica-safe) and REUSED on a resumed /upload/init so
    # the browser keeps staging blocks to the same blob — a fresh UUID per call
    # would orphan already-staged blocks (InvalidBlockList) and break retry/resume.
    anonymous_blob_name = None
    if is_fresh_upload and direct:
        # Fast path for the common case (new upload, direct-to-blob): one read
        # + one write against the metadata row instead of reset_received_ranges
        # and reserve_pending_anonymous_blob each separately re-reading and
        # re-writing it (was 2 reads + 2 writes here alone).
        try:
            anonymous_blob_name = app.reset_upload_tracking_and_reserve_blob(
                user_id, filename, total_size, expected_hash or None,
            )
        except Exception:
            app.app.logger.debug('Failed to reset tracking / reserve anonymous blob name for %s', filename)
    else:
        if is_fresh_upload:
            try:
                app.reset_received_ranges(user_id, filename, total_size, expected_hash or None)
            except Exception:
                pass
        if direct:
            try:
                anonymous_blob_name = app.reserve_pending_anonymous_blob(user_id, filename, expected_hash or None)
            except Exception:
                app.app.logger.debug('Failed to reserve anonymous blob name for %s', filename)

    blob_url = None
    expires_at = None
    if direct:
        try:
            # Use anonymous_blob_name for the SAS URL if available
            blob_url, expires_at = app._create_direct_upload_blob_url(anonymous_blob_name or filename)
        except Exception as exc:
            app.app.logger.exception('Failed to create direct upload SAS for %s', filename)
            return app.jsonify({'error': 'Direct upload is not configured'}), 503
    thumbnail_blob_url = None
    thumbnail_sas_expires_at = None
    try:
        # Mint the thumbnail SAS under the same anonymous blob name as the image so
        # the browser's direct thumbnail upload also lands on the anonymized blob.
        thumbnail_blob_url, thumbnail_sas_expires_at = app._create_direct_thumbnail_upload_blob_url(anonymous_blob_name or filename)
    except Exception:
        pass
    return app.jsonify({
        'uploadId': upload_id,
        'uploadUrl': f'/upload/{upload_id}?filename={filename}',
        'blobUrl': blob_url,
        'thumbnailBlobUrl': thumbnail_blob_url,
        'blobName': anonymous_blob_name or filename,
        'originalFilename': filename,
        'sasExpiresAt': expires_at,
        'thumbnailSasExpiresAt': thumbnail_sas_expires_at,
        'totalSize': total_size,
    })

@upload_bp.route('/upload/init-batch', methods=['POST'])
@upload_bp.route('/upload/init-batch/', methods=['POST'])
@upload_bp.route('/api/upload/init-batch', methods=['POST'])
@upload_bp.route('/api/upload/init-batch/', methods=['POST'])
def init_upload_batch():
    """Batched /upload/init for a chunk of new (non-resumed) direct-to-blob
    uploads: one Azure Table query + one transactional batch write for the
    whole chunk, instead of a read + write PER FILE (see
    reset_upload_tracking_and_reserve_blobs_batch). Meant to be called once
    per ~10-15-file chunk from the frontend, not once per file.

    Scoped to fresh, direct-to-blob uploads only -- deliberately simpler than
    single-file /upload/init: no per-file _cleanup_failed_upload rare-path
    handling (re-uploading a filename that was already fully completed
    before, without deleting the old photo first, is rare enough that the
    frontend falls back to single-file /upload/init for resumes and any
    retried file anyway). Every entry must be a genuinely new upload -- pass
    a client-supplied uploadId through single-file /upload/init instead.
    """
    request_started = app.time.monotonic()
    phase_ms: app.Dict[str, int] = {}
    phase_started = request_started
    def _mark(phase: str) -> None:
        nonlocal phase_started
        now = app.time.monotonic()
        phase_ms[phase] = round((now - phase_started) * 1000)
        phase_started = now

    user_id, error = app._require_user_id()
    if error:
        return error
    blocked = app._library_cleanup_block_reason(user_id)
    if blocked:
        return app.jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    _mark('auth_ms')
    data = app.request.get_json(silent=True) or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return app.jsonify({'error': 'files must be a non-empty list'}), 400
    if len(files) > app.MAX_INIT_BATCH_FILES:
        return app.jsonify({'error': f'Batch too large (max {app.MAX_INIT_BATCH_FILES} files)'}), 400

    parsed = []
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            parsed.append({'index': idx, 'error': 'Invalid file entry'})
            continue
        filename = app._validate_media_filename(item.get('filename', ''))
        total_size = int(item.get('totalSize', 0) or 0)
        expected_hash = str(item.get('sha256') or '').strip()
        if not filename:
            parsed.append({'index': idx, 'error': 'Invalid filename'})
            continue
        if total_size <= 0 or total_size > app.MAX_UPLOAD_FILE_BYTES:
            parsed.append({'index': idx, 'filename': filename, 'error': 'Invalid totalSize'})
            continue
        parsed.append({
            'index': idx,
            'filename': filename,
            'total_size': total_size,
            'expected_hash': expected_hash,
            'upload_id': str(app.uuid.uuid4()),
        })
    _mark('validate_ms')

    valid = [p for p in parsed if 'error' not in p]
    anonymous_blob_names: app.Dict[str, str] = {}
    if valid:
        try:
            anonymous_blob_names = app.reset_upload_tracking_and_reserve_blobs_batch(
                user_id,
                [
                    {
                        'filename': p['filename'],
                        'total_size': p['total_size'],
                        'expected_hash': p['expected_hash'] or None,
                        'is_fresh': True,
                    }
                    for p in valid
                ],
            )
        except Exception:
            app.app.logger.exception('Batch upload-tracking reservation failed for %s files', len(valid))
    _mark('batch_reserve_ms')

    results = []
    for p in parsed:
        if 'error' in p:
            results.append({'index': p['index'], 'filename': p.get('filename'), 'error': p['error']})
            continue
        filename = p['filename']
        anonymous_blob_name = anonymous_blob_names.get(filename)
        try:
            blob_url, expires_at = app._create_direct_upload_blob_url(anonymous_blob_name or filename)
        except Exception as exc:
            app.app.logger.exception('Direct upload blob URL creation failed')
            results.append({'index': p['index'], 'filename': filename, 'error': 'Direct upload is not configured'})
            continue
        thumbnail_blob_url = None
        thumbnail_sas_expires_at = None
        try:
            thumbnail_blob_url, thumbnail_sas_expires_at = app._create_direct_thumbnail_upload_blob_url(anonymous_blob_name or filename)
        except Exception:
            pass
        results.append({
            'index': p['index'],
            'filename': filename,
            'uploadId': p['upload_id'],
            'uploadUrl': f"/upload/{p['upload_id']}?filename={filename}",
            'blobUrl': blob_url,
            'thumbnailBlobUrl': thumbnail_blob_url,
            'blobName': anonymous_blob_name or filename,
            'originalFilename': filename,
            'sasExpiresAt': expires_at,
            'thumbnailSasExpiresAt': thumbnail_sas_expires_at,
            'totalSize': p['total_size'],
        })
    _mark('sas_mint_ms')
    app.app.logger.info(
        'init-batch timings user=%s files=%s phase_ms=%s total_ms=%s',
        user_id, len(files), phase_ms, round((app.time.monotonic() - request_started) * 1000),
    )
    return app.jsonify({'results': results})

@upload_bp.route('/upload/known-hashes', methods=['GET'])
@upload_bp.route('/upload/known-hashes/', methods=['GET'])
@upload_bp.route('/api/upload/known-hashes', methods=['GET'])
@upload_bp.route('/api/upload/known-hashes/', methods=['GET'])
def get_known_upload_hashes():
    """One bulk fetch for the whole library's dedup index -- meant to be called
    once per upload batch (see list_known_file_hashes), not per file, so the
    frontend can skip re-uploading a known duplicate before spending any
    transfer bandwidth on it."""
    _, user_id, error = app._require_library_context()
    if error:
        return error
    return app.jsonify({'hashes': app.list_known_file_hashes(user_id)})

@upload_bp.route('/upload/finalize', methods=['POST'])
@upload_bp.route('/upload/finalize/', methods=['POST'])
@upload_bp.route('/api/upload/finalize', methods=['POST'])
@upload_bp.route('/api/upload/finalize/', methods=['POST'])
def finalize_direct_upload():
    account_id, user_id, error = app._require_library_context()
    if error:
        return error
    blocked = app._library_cleanup_block_reason(user_id)
    if blocked:
        return app.jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0) or 0)
    content_type = str(data.get('contentType') or 'application/octet-stream')
    if not filename:
        return app.jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0 or total_size > app.MAX_UPLOAD_FILE_BYTES:
        return app.jsonify({'error': 'Invalid totalSize'}), 400

    # Prefer the blob name the browser itself got back from /upload/init and
    # actually staged its blocks against, over re-deriving it from the shared
    # (user, filename) metadata row: when several files share an original
    # filename (e.g. many photos named "Ip_image.jpeg"), every one of their
    # /upload/init calls reserves its OWN blob but writes it onto that same
    # row -- renaming apart into distinct rows only happens later, inside
    # finalize_uploaded_file below. Re-deriving from the row here would pick
    # up whichever file's init call happened to run last, not necessarily
    # this one, causing spurious "Uploaded blob not found"/"size mismatch"
    # for files that lose that race. Falls back to the row lookup for
    # sessions that predate this field (durable + replica-safe, so it still
    # works even when finalize lands on a different replica than init).
    anonymous_blob_name = app._validate_client_blob_name(data.get('blobName')) or app.read_pending_anonymous_blob(user_id, filename)
    blob_to_check = anonymous_blob_name or filename

    try:
        props = app.blob_service_client.get_blob_client(container=app.BLOB_IMAGE_CONTAINER, blob=blob_to_check).get_blob_properties()
        if int(getattr(props, 'size', 0) or 0) != total_size:
            return app.jsonify({'error': 'Uploaded blob size mismatch'}), 409
    except Exception as exc:
        app.app.logger.exception('Uploaded blob property check failed')
        return app.jsonify({'error': 'Uploaded blob not found'}), 404

    try:
        duplicates, final_name = app.finalize_uploaded_file(
            user_id,
            filename,
            content_type,
            client_processing=data.get('clientProcessing'),
            client_processing_report=data.get('clientProcessingReport'),
            client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
            client_sha256=str(data.get('sha256') or ''),
            anonymous_blob_name=anonymous_blob_name,
        )
    except Exception as exc:
        app.app.logger.exception('Direct upload finalization failed for %s', filename)
        return app.jsonify({'error': 'Upload finalization failed'}), 500
    # finalize_uploaded_file writes metadata via storage_utils (bypassing
    # _update_metadata_entity_fields), so drop the scan cache explicitly: the
    # gallery refetches right after an upload and must see the new photo.
    app._invalidate_metadata_scan_cache(user_id)
    # Persist the blob size (validated above) and the adding account in one write:
    # the size lets the gallery listing skip a per-photo blob HEAD, and uploadedBy
    # attributes the photo to the library member who added it.
    try:
        finalize_updates: app.Dict[str, object] = {'size': total_size}
        if account_id:
            finalize_updates['uploadedBy'] = account_id
        client_last_modified_iso = app.epoch_millis_to_iso(data.get('clientLastModified'))
        if client_last_modified_iso:
            finalize_updates['clientLastModified'] = client_last_modified_iso
        app._update_metadata_entity_fields(user_id, final_name, finalize_updates)
    except Exception:
        app.app.logger.debug('Could not stamp finalize metadata for %s', final_name)
    metadata = None
    try:
        metadata = app.metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
        if metadata.get('upload_sha256_expected') and metadata.get('upload_sha256_match') is False:
            return app.jsonify({
                'error': 'Upload hash mismatch',
                'filename': final_name,
                'uploadSha256Match': metadata.get('upload_sha256_match'),
            }), 422
    except Exception:
        pass
    if data.get('clientProcessing') or data.get('clientProcessingReport'):
        try:
            metadata = app.apply_client_processing_results_for_file(
                user_id,
                final_name,
                client_processing=data.get('clientProcessing'),
                client_processing_report=data.get('clientProcessingReport'),
                client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
            )
        except Exception:
            app.app.logger.exception('Inline client processing update failed for %s', final_name)
    try:
        metadata = metadata or app.metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
    except Exception:
        pass
    try:
        app._queue_upload_processing(user_id, final_name)
    except Exception:
        app.app.logger.exception('Failed to queue post-finalize processing for %s', final_name)
    try:
        app._mark_fresh_upload_activity(user_id)
    except Exception:
        app.app.logger.exception('Failed to record fresh upload activity for %s', user_id)
    try:
        app._queue_people_clustering_after_face_processing(user_id, final_name, metadata)
    except Exception:
        app.app.logger.exception('Failed to auto-queue clustering for %s', final_name)
    return app.jsonify({
        'uploadId': data.get('uploadId') or '',
        'filename': final_name,
        'bytesReceived': total_size,
        'totalSize': total_size,
        'complete': True,
        'duplicates': duplicates,
        'clientProcessingLateResultWaitSeconds': 0,
    })

@upload_bp.route('/upload/finalize-batch', methods=['POST'])
@upload_bp.route('/upload/finalize-batch/', methods=['POST'])
@upload_bp.route('/api/upload/finalize-batch', methods=['POST'])
@upload_bp.route('/api/upload/finalize-batch/', methods=['POST'])
def finalize_upload_batch():
    """Batched /upload/finalize for a chunk of just-committed direct-to-blob
    uploads -- same reasoning as /upload/init-batch
    (reset_upload_tracking_and_reserve_blobs_batch), applied to finalize.

    Each individual finalize_uploaded_file call is inherently slow (dedup
    check, metadata write, queue enqueue -- tens of seconds under load,
    see docs/ipworker-architecture.md-adjacent upload-speed investigation),
    and one HTTP request per file meant one gunicorn thread held for that
    whole duration per file. Under concurrent load this both exhausted the
    fleet's thread pool (requests queueing behind each other) and put
    multiple finalize calls in real Python-level GIL contention with each
    other (each concurrently executing real work, not just waiting on I/O).
    Looping sequentially through a chunk on ONE thread inside ONE request
    fixes both: one thread instead of N, and no concurrent execution within
    this chunk to contend over. finalize_uploaded_file itself is untouched --
    this only changes how many HTTP requests/threads are spent invoking it,
    not what it does per file.

    Every entry must be a genuinely new (non-resumed) direct-to-blob upload,
    same restriction as /upload/init-batch -- a resumed upload still uses
    single-file /upload/finalize.
    """
    request_started = app.time.monotonic()
    account_id, user_id, error = app._require_library_context()
    if error:
        return error
    blocked = app._library_cleanup_block_reason(user_id)
    if blocked:
        return app.jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    auth_ms = round((app.time.monotonic() - request_started) * 1000)
    data = app.request.get_json(silent=True) or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return app.jsonify({'error': 'files must be a non-empty list'}), 400
    if len(files) > app.MAX_INIT_BATCH_FILES:
        return app.jsonify({'error': f'Batch too large (max {app.MAX_INIT_BATCH_FILES} files)'}), 400

    try:
        app._mark_fresh_upload_activity(user_id)
    except Exception:
        app.app.logger.exception('Failed to record fresh upload activity for %s', user_id)

    # Summed across every file in the batch, then logged once at the end
    # (instead of once per file) so a large batch under concurrent load
    # doesn't multiply log volume at exactly the concurrency level this is
    # meant to help measure.
    phase_totals_ms = {
        'blob_check': 0, 'finalize_write': 0, 'metadata_stamp': 0,
        'metadata_read': 0, 'client_processing': 0, 'queue': 0, 'clustering_queue': 0,
    }

    def _accum(key: str, start: float) -> float:
        now = app.time.monotonic()
        phase_totals_ms[key] += round((now - start) * 1000)
        return now

    results = []
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            results.append({'index': idx, 'error': 'Invalid file entry'})
            continue
        filename = app._validate_media_filename(item.get('filename', ''))
        total_size = int(item.get('totalSize', 0) or 0)
        content_type = str(item.get('contentType') or 'application/octet-stream')
        if not filename:
            results.append({'index': idx, 'error': 'Invalid filename'})
            continue
        if total_size <= 0 or total_size > app.MAX_UPLOAD_FILE_BYTES:
            results.append({'index': idx, 'filename': filename, 'error': 'Invalid totalSize'})
            continue

        t = app.time.monotonic()
        # See the matching comment in finalize_direct_upload above -- same
        # same-filename-collision race, same fix.
        anonymous_blob_name = app._validate_client_blob_name(item.get('blobName')) or app.read_pending_anonymous_blob(user_id, filename)
        blob_to_check = anonymous_blob_name or filename
        try:
            props = app.blob_service_client.get_blob_client(container=app.BLOB_IMAGE_CONTAINER, blob=blob_to_check).get_blob_properties()
            if int(getattr(props, 'size', 0) or 0) != total_size:
                t = _accum('blob_check', t)
                results.append({'index': idx, 'filename': filename, 'error': 'Uploaded blob size mismatch'})
                continue
        except Exception as exc:
            t = _accum('blob_check', t)
            app.app.logger.exception('Batch upload blob property check failed')
            results.append({'index': idx, 'filename': filename, 'error': 'Uploaded blob not found'})
            continue
        t = _accum('blob_check', t)

        try:
            duplicates, final_name = app.finalize_uploaded_file(
                user_id,
                filename,
                content_type,
                client_processing=item.get('clientProcessing'),
                client_processing_report=item.get('clientProcessingReport'),
                client_asset_id=str(item.get('clientAssetId') or item.get('uploadId') or ''),
                client_sha256=str(item.get('sha256') or ''),
                anonymous_blob_name=anonymous_blob_name,
            )
        except Exception as exc:
            t = _accum('finalize_write', t)
            app.app.logger.exception('Batch finalize failed for %s', filename)
            results.append({'index': idx, 'filename': filename, 'error': 'Upload finalization failed'})
            continue
        t = _accum('finalize_write', t)

        # Same per-file follow-up as single-file finalize above, just inline
        # in this loop instead of a separate request.
        app._invalidate_metadata_scan_cache(user_id)
        try:
            finalize_updates: app.Dict[str, object] = {'size': total_size}
            if account_id:
                finalize_updates['uploadedBy'] = account_id
            client_last_modified_iso = app.epoch_millis_to_iso(item.get('clientLastModified'))
            if client_last_modified_iso:
                finalize_updates['clientLastModified'] = client_last_modified_iso
            app._update_metadata_entity_fields(user_id, final_name, finalize_updates)
        except Exception:
            app.app.logger.debug('Could not stamp finalize metadata for %s', final_name)
        t = _accum('metadata_stamp', t)

        metadata = None
        hash_mismatch = False
        try:
            metadata = app.metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
            if metadata.get('upload_sha256_expected') and metadata.get('upload_sha256_match') is False:
                hash_mismatch = True
        except Exception:
            pass
        t = _accum('metadata_read', t)
        if hash_mismatch:
            results.append({
                'index': idx,
                'filename': final_name,
                'error': 'Upload hash mismatch',
                'uploadSha256Match': metadata.get('upload_sha256_match') if metadata else False,
            })
            continue

        if item.get('clientProcessing') or item.get('clientProcessingReport'):
            try:
                metadata = app.apply_client_processing_results_for_file(
                    user_id,
                    final_name,
                    client_processing=item.get('clientProcessing'),
                    client_processing_report=item.get('clientProcessingReport'),
                    client_asset_id=str(item.get('clientAssetId') or item.get('uploadId') or ''),
                )
            except Exception:
                app.app.logger.exception('Inline client processing update failed for %s', final_name)
        t = _accum('client_processing', t)
        try:
            metadata = metadata or app.metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
        except Exception:
            pass
        t = _accum('metadata_read', t)
        try:
            app._queue_upload_processing(user_id, final_name)
        except Exception:
            app.app.logger.exception('Failed to queue post-finalize processing for %s', final_name)
        t = _accum('queue', t)
        try:
            app._queue_people_clustering_after_face_processing(user_id, final_name, metadata)
        except Exception:
            app.app.logger.exception('Failed to auto-queue clustering for %s', final_name)
        t = _accum('clustering_queue', t)

        results.append({
            'index': idx,
            'uploadId': item.get('uploadId') or '',
            'filename': final_name,
            'bytesReceived': total_size,
            'totalSize': total_size,
            'complete': True,
            'duplicates': duplicates,
            'clientProcessingLateResultWaitSeconds': 0,
        })

    app.app.logger.info(
        'finalize-batch timings user=%s files=%s auth_ms=%s phase_totals_ms=%s total_ms=%s',
        user_id, len(files), auth_ms, phase_totals_ms, round((app.time.monotonic() - request_started) * 1000),
    )
    return app.jsonify({'results': results})

@upload_bp.route('/upload/client-processing', methods=['POST'])
@upload_bp.route('/upload/client-processing/', methods=['POST'])
@upload_bp.route('/api/upload/client-processing', methods=['POST'])
@upload_bp.route('/api/upload/client-processing/', methods=['POST'])
def upload_client_processing_results():
    request_started = app.time.monotonic()
    user_id, error = app._require_user_id()
    if error:
        return error
    blocked = app._library_cleanup_block_reason(user_id)
    if blocked:
        return app.jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    auth_ms = round((app.time.monotonic() - request_started) * 1000)
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(data.get('filename', ''))
    if not filename:
        return app.jsonify({'error': 'Invalid filename'}), 400
    claimed_steps_raw = data.get('claimedSteps')
    claimed_steps = (
        [str(s).strip() for s in claimed_steps_raw if str(s).strip()]
        if isinstance(claimed_steps_raw, list) else None
    )
    t = app.time.monotonic()
    try:
        metadata = app.apply_client_processing_results_for_file(
            user_id,
            filename,
            client_processing=data.get('clientProcessing'),
            client_processing_report=data.get('clientProcessingReport'),
            client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
            thumbnail_already_uploaded=bool(data.get('thumbnailAlreadyUploaded')),
            claimed_steps=claimed_steps,
        )
    except Exception as exc:
        app.app.logger.exception('Late browser processing update failed for %s', filename)
        message = str(exc)
        if 'deleted' in message.lower():
            return app.jsonify({'error': 'Photo has been deleted'}), 410
        return app.jsonify({'error': 'Client processing update failed'}), 500
    apply_ms = round((app.time.monotonic() - t) * 1000)

    # apply_client_processing_results_for_file writes via storage_utils,
    # bypassing _update_metadata_entity_fields.
    app._invalidate_metadata_scan_cache(user_id)

    t = app.time.monotonic()
    try:
        app._queue_people_clustering_after_face_processing(user_id, filename, metadata)
    except Exception:
        app.app.logger.exception('Failed to auto-queue clustering after browser processing update for %s', filename)
    clustering_queue_ms = round((app.time.monotonic() - t) * 1000)

    app.app.logger.info(
        'client-processing timings user=%s file=%s auth_ms=%s apply_ms=%s clustering_queue_ms=%s total_ms=%s',
        user_id, filename, auth_ms, apply_ms, clustering_queue_ms,
        round((app.time.monotonic() - request_started) * 1000),
    )
    return app.jsonify({
        'uploadId': data.get('uploadId') or '',
        'filename': filename,
        'accepted': True,
        'statuses': {
            'preview': metadata.get('preview_status'),
            'thumbnail': metadata.get('thumbnail_status'),
            'face': metadata.get('face_status'),
            'aiVision': metadata.get('ai_vision_status'),
            'mapDetection': metadata.get('map_detection_status'),
            'exif': metadata.get('exif_status'),
            'ocr': metadata.get('ocr_status'),
        },
    })

@upload_bp.route('/upload/processing/pending', methods=['GET'])
@upload_bp.route('/upload/processing/pending/', methods=['GET'])
@upload_bp.route('/api/upload/processing/pending', methods=['GET'])
@upload_bp.route('/api/upload/processing/pending/', methods=['GET'])
def upload_processing_pending():
    user_id, error = app._require_user_id()
    if error:
        return error

    if app.metadata_table_client is None:
        app.app.logger.warning('Browser processing pending requested before metadata table was configured.')
        return app.jsonify({'pending': []})

    try:
        # Raised from 25: the frontend's pending-drain now fetches a real batch
        # (PENDING_PROCESSING_BATCH_SIZE=40, see AppServicesProvider.tsx) up
        # front for its lanes to work through, decoupled from lane count --
        # capping this below that silently truncated the batch every time.
        limit = max(1, min(int(app.request.args.get('limit', '1') or 1), 60))
    except ValueError:
        return app.jsonify({'error': 'Invalid limit'}), 400

    try:
        entities = app._query_metadata_rows_for_user(
            user_id,
            select=app.BROWSER_PROCESSING_PENDING_SELECT,
            purpose='browser_processing_pending',
        )
    except Exception as exc:
        app.app.logger.warning('Browser processing pending scan failed for %s: %s', user_id, exc, exc_info=True)
        return app.jsonify({'pending': []})

    pending = []
    for entity in entities:
        item = app._browser_processing_pending_item(entity)
        if item:
            pending.append(item)
    pending.sort(key=lambda item: str(item.get('lastProcessingUpdate') or ''))
    bounded = pending[:limit]
    for item in bounded:
        # Mint SAS against the physical blob (anonymous UUID for anonymized photos),
        # then drop the internal marker so it isn't exposed to the browser.
        physical_name = item.pop('_blobName', None) or item['filename']
        try:
            url, expires_at = app._create_scoped_blob_url(app.BLOB_IMAGE_CONTAINER, physical_name, minutes=10)
            item['sourceUrl'] = url
            item['sourceExpiresAt'] = expires_at
        except Exception:
            app.app.logger.warning('Failed to mint browser processing source URL for %s', item.get('filename'), exc_info=True)
        try:
            thumbnail_url, thumbnail_expires_at = app._create_direct_thumbnail_upload_blob_url(physical_name)
            item['thumbnailUploadUrl'] = thumbnail_url
            item['thumbnailUploadExpiresAt'] = thumbnail_expires_at
        except Exception:
            app.app.logger.warning('Failed to mint browser thumbnail upload URL for %s', item.get('filename'), exc_info=True)
    return app.jsonify({'pending': bounded, 'totalPending': len(pending)})

@upload_bp.route('/upload/processing/claim', methods=['POST'])
@upload_bp.route('/upload/processing/claim/', methods=['POST'])
@upload_bp.route('/api/upload/processing/claim', methods=['POST'])
@upload_bp.route('/api/upload/processing/claim/', methods=['POST'])
def upload_processing_claim():
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(str(data.get('filename') or '')) or ''
    if not filename:
        return app.jsonify({'error': 'Missing filename'}), 400
    lease_owner = str(data.get('leaseId') or data.get('ownerId') or f'browser-{app.uuid.uuid4()}').strip()
    requested_steps = data.get('steps')
    steps = [str(step or '').strip() for step in requested_steps] if isinstance(requested_steps, list) else None
    response, status = app._claim_processing_lease_response(user_id, filename, lease_owner, steps, data.get('blobName'))
    return app.jsonify(response), status

@upload_bp.route('/upload/processing/claim-batch', methods=['POST'])
@upload_bp.route('/upload/processing/claim-batch/', methods=['POST'])
@upload_bp.route('/api/upload/processing/claim-batch', methods=['POST'])
@upload_bp.route('/api/upload/processing/claim-batch/', methods=['POST'])
def upload_processing_claim_batch():
    """Claim leases for several photos in one round trip.

    Scoped deliberately to "claim what you're about to start on right now"
    (the frontend only ever batches this to its current lane count, not its
    whole fetched pending batch) -- claiming far more than that upfront would
    hold leases on photos no lane has reached yet, and could let them expire
    before anyone actually processes them. Each item's claim is still fully
    independent (own read/write, own success/failure), same as calling
    /upload/processing/claim in a loop; this only collapses the HTTP round
    trips, not the lease semantics.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return app.jsonify({'error': 'items must be a non-empty list'}), 400
    if len(items) > app.MAX_CLAIM_BATCH_ITEMS:
        return app.jsonify({'error': f'Too many items (max {app.MAX_CLAIM_BATCH_ITEMS})'}), 400

    results = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            results.append({'claimed': False, 'reason': 'invalid_item'})
            continue
        filename = app._validate_media_filename(str(raw_item.get('filename') or '')) or ''
        if not filename:
            results.append({'claimed': False, 'reason': 'invalid_filename'})
            continue
        lease_owner = str(raw_item.get('leaseId') or raw_item.get('ownerId') or f'browser-{app.uuid.uuid4()}').strip()
        requested_steps = raw_item.get('steps')
        steps = [str(step or '').strip() for step in requested_steps] if isinstance(requested_steps, list) else None
        response, _status = app._claim_processing_lease_response(user_id, filename, lease_owner, steps, raw_item.get('blobName'))
        results.append({'filename': filename, **response})

    return app.jsonify({'results': results})

@upload_bp.route('/upload/processing/heartbeat', methods=['POST'])
@upload_bp.route('/upload/processing/heartbeat/', methods=['POST'])
@upload_bp.route('/api/upload/processing/heartbeat', methods=['POST'])
@upload_bp.route('/api/upload/processing/heartbeat/', methods=['POST'])
def upload_processing_heartbeat():
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(str(data.get('filename') or '')) or ''
    lease_id = str(data.get('leaseId') or '')
    try:
        lease = app.heartbeat_processing_lease(user_id, filename, lease_id, lease_seconds=120)
    except Exception as exc:
        app.app.logger.exception('Processing lease heartbeat failed')
        return app.jsonify({'ok': False, 'reason': 'lease_missing'}), 409
    return app.jsonify({'ok': True, 'expiresAt': lease.get('leaseExpiresAt') or ''})

@upload_bp.route('/upload/processing/release', methods=['POST'])
@upload_bp.route('/upload/processing/release/', methods=['POST'])
@upload_bp.route('/api/upload/processing/release', methods=['POST'])
@upload_bp.route('/api/upload/processing/release/', methods=['POST'])
def upload_processing_release():
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    filename = app._validate_media_filename(str(data.get('filename') or '')) or ''
    lease_id = str(data.get('leaseId') or '')
    app.release_processing_lease(user_id, filename, lease_id)
    return app.jsonify({'ok': True})

@upload_bp.route('/upload/cancel', methods=['POST'])
@upload_bp.route('/upload/cancel/', methods=['POST'])
@upload_bp.route('/api/upload/cancel', methods=['POST'])
@upload_bp.route('/api/upload/cancel/', methods=['POST'])
def cancel_uploads():
    user_id, error = app._require_user_id()
    if error:
        return error

    data = app.request.get_json(silent=True) or {}
    files = data.get('files', [])
    if not isinstance(files, list) or not files:
        return app.jsonify({'error': 'files must be a non-empty list'}), 400

    cleaned = []
    errors = []
    for item in files:
        if not isinstance(item, dict):
            errors.append({'filename': '<unknown>', 'error': 'Invalid file entry'})
            continue

        original_name = str(item.get('filename') or '')
        safe_name = app._validate_media_filename(original_name)
        if not safe_name:
            errors.append({'filename': original_name or '<unknown>', 'error': 'Invalid filename'})
            continue

        result = app._cleanup_failed_upload(user_id, safe_name, str(item.get('uploadId') or ''))
        cleaned.append(result)
        if result['errors']:
            errors.append({'filename': safe_name, 'error': '; '.join(result['errors'])})

    return app.jsonify({
        'success': len(errors) == 0,
        'cleaned': cleaned,
        'errors': errors,
    }), 200 if len(errors) == 0 else 207

@upload_bp.route('/uploads/corrupted', methods=['GET'])
@upload_bp.route('/uploads/corrupted/', methods=['GET'])
@upload_bp.route('/api/uploads/corrupted', methods=['GET'])
@upload_bp.route('/api/uploads/corrupted/', methods=['GET'])
def list_corrupted_uploads():
    user_id, error = app._require_user_id()
    if error:
        return error
    try:
        rows = app._cached_metadata_rows_for_user(user_id, purpose='uploads.corrupted')
    except Exception as exc:
        app.app.logger.exception('Corrupted uploads metadata read failed')
        return app.jsonify({'error': 'Unable to read photo metadata.'}), 503

    items = []
    for row in rows:
        if row.get('verification_status') != 'failed' and not row.get('corrupted'):
            continue
        filename = row.get('RowKey')
        if not filename:
            continue

        reason = row.get('verification_error') or row.get('last_error') or ''
        sha256_match = row.get('upload_sha256_match')
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        reason_lower = str(reason).lower()
        raw_integrity_error = (
            'too small' in reason_lower
            or 'header' in reason_lower
            or 'signature' in reason_lower
            or 'decode' in reason_lower
            or 'embedded preview' in reason_lower
            or 'sha256' in reason_lower
        )
        if ext in app.RAW_EXTENSIONS_RAWPY and not raw_integrity_error and not (sha256_match is False or sha256_match == 'false'):
            continue
        if sha256_match is False or sha256_match == 'false':
            corruption_type = 'hash_mismatch'
        elif reason:
            corruption_type = 'parse_error'
        else:
            corruption_type = 'unknown'

        media_urls = app._private_photo_media_urls(filename, row)
        items.append({
            'filename': filename,
            'reason': reason,
            'corruptionType': corruption_type,
            'uploadedAt': row.get('uploadDate') or '',
            'mimeType': row.get('mimeType') or '',
            'thumbnailUrl': media_urls['thumbnailUrl'],
            'url': media_urls['url'],
            'rotation': app._normalize_rotation(row.get('rotation', 0)),
            'verificationStatus': row.get('verification_status') or '',
            'sha256Match': sha256_match,
        })

    items.sort(key=lambda item: item.get('uploadedAt') or '', reverse=True)
    return app.jsonify({'items': items, 'count': len(items)})

@upload_bp.route('/uploads/corrupted/<path:filename>/clear', methods=['POST'])
@upload_bp.route('/api/uploads/corrupted/<path:filename>/clear', methods=['POST'])
def clear_corrupted_upload(filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400

    metadata = app._get_metadata_entity(user_id, safe_name)
    if metadata is None:
        return app.jsonify({'error': 'Not found'}), 404

    try:
        app.download_media_bytes('image', app._blob_name_from_metadata(metadata, safe_name))
    except Exception:
        return app.jsonify({'error': 'Image file not found'}), 404

    metadata['corrupted'] = False
    metadata.pop('verification_error', None)
    metadata.pop('corrupted_at', None)
    if metadata.get('verification_status') == 'failed':
        metadata['verification_status'] = 'pending'
    app.metadata_table_client.upsert_entity(metadata)
    app._invalidate_metadata_scan_cache(user_id)

    return app.jsonify({
        'filename': safe_name,
        'corrupted': False,
        'thumbnailRegenerated': False,
    })

@upload_bp.route('/upload/processing/status', methods=['GET'])
@upload_bp.route('/upload/processing/status/', methods=['GET'])
@upload_bp.route('/api/upload/processing/status', methods=['GET'])
@upload_bp.route('/api/upload/processing/status/', methods=['GET'])
def processing_status():
    user_id, error = app._require_user_id()
    if error:
        return error
    counts = app._count_processing_statuses(user_id, ['preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'])

    def _pending(summary: app.Dict[str, int]) -> int:
        return int(summary.get('queued', 0) or 0) + int(summary.get('pending', 0) or 0)

    def _build(key: str) -> app.Dict:
        summary = counts.get(key, {})
        return {
            'queued': int(summary.get('queued', 0) or 0),
            'pending': int(summary.get('pending', 0) or 0),
            'pendingTotal': _pending(summary),
            'running': int(summary.get('running', 0) or 0),
            'failed': int(summary.get('failed', 0) or 0),
            'noData': int(summary.get('no_data', 0) or 0),
        }

    response = app.jsonify({
        'generatedAt': app.datetime.now(app.timezone.utc).isoformat(),
        'preview': _build('preview'),
        'thumbnail': _build('thumbnail'),
        'exif': _build('exif'),
        'ocr': _build('ocr'),
        'ai_vision': _build('ai_vision'),
        'map_detection': _build('map_detection'),
        'face': _build('face'),
    })
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
