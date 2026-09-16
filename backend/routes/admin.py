"""Blueprint: admin routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/api/admin/people/recluster', methods=['POST'])
@admin_bp.route('/admin/people/recluster', methods=['POST'])
def admin_recluster_people():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        data = app.request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'RECLUSTER_REPAIR':
            return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        queued = app._enqueue_clustering_job(
            user_id,
            force=False,
            job_type='people_recluster',
            allow_reassign_confirmed=app._coerce_bool(data.get('allowReassignConfirmed', False)),
        )
        response = app._clustering_queue_response(queued)
        if queued.get('status') == 'unavailable':
            return app.jsonify(response), 503
        if queued.get('status') == 'failed':
            return app.jsonify(response), 500
        return app.jsonify(response)
    except Exception as exc:
        app.app.logger.exception('Admin people recluster route failed')
        return app.jsonify({'error': 'Admin people recluster failed'}), 500

@admin_bp.route('/api/admin/people/recluster/restore', methods=['POST'])
@admin_bp.route('/admin/people/recluster/restore', methods=['POST'])
def restore_people_recluster_snapshot():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    snapshot_id = str(data.get('snapshotId') or '').strip()
    if not snapshot_id:
        return app.jsonify({'error': 'snapshotId required'}), 400
    result = app._restore_people_repair_snapshot(user_id, snapshot_id)
    if not result.get('success'):
        return app.jsonify(result), 404
    return app.jsonify(result)

@admin_bp.route('/api/admin/people/dedupe-faces', methods=['POST'])
@admin_bp.route('/admin/people/dedupe-faces', methods=['POST'])
def admin_dedupe_faces():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'DEDUPE_FACES':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = app._coerce_bool(data.get('dryRun', True))
    queued = app._enqueue_admin_repair_job(user_id, action='dedupe_faces', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/people/suppress-suspicious-faces', methods=['POST'])
@admin_bp.route('/admin/people/suppress-suspicious-faces', methods=['POST'])
def admin_suppress_suspicious_faces():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'SUPPRESS_SUSPICIOUS_FACES':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = app._coerce_bool(data.get('dryRun', True))
    queued = app._enqueue_admin_repair_job(user_id, action='suppress_suspicious', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/people/unblock-low-confidence-faces', methods=['POST'])
@admin_bp.route('/admin/people/unblock-low-confidence-faces', methods=['POST'])
def admin_unblock_low_confidence_faces():
    """Un-reject faces that were auto-suppressed as low-confidence but now meet
    the current (lowered) threshold. Run this after lowering
    FACE_LOW_CONFIDENCE_REJECT_BELOW to bring previously-rejected faces back
    into clustering."""
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'UNBLOCK_LOW_CONFIDENCE_FACES':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = app._coerce_bool(data.get('dryRun', True))
    queued = app._enqueue_admin_repair_job(user_id, action='unblock_low_confidence', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/people/rebuild-photo-people-index', methods=['POST'])
@admin_bp.route('/admin/people/rebuild-photo-people-index', methods=['POST'])
def admin_rebuild_photo_people_index():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'REBUILD_PEOPLE_INDEX':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = app._coerce_bool(data.get('dryRun', True))
    queued = app._enqueue_admin_repair_job(user_id, action='rebuild_people_index', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/vector-index/rebuild', methods=['POST'])
@admin_bp.route('/admin/vector-index/rebuild', methods=['POST'])
def admin_rebuild_vector_index():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        data = app.request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'REBUILD_VECTOR_INDEX':
            return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        queued = app._enqueue_clustering_job(user_id, force=True, job_type='vector_index_rebuild')
        return app.jsonify(app._clustering_queue_response(queued))
    except Exception as exc:
        app.app.logger.exception('Admin vector index rebuild enqueue failed')
        return app.jsonify({'error': 'Admin vector index rebuild failed'}), 500

@admin_bp.route('/api/admin/people/repair-stale-memberships', methods=['POST'])
@admin_bp.route('/admin/people/repair-stale-memberships', methods=['POST'])
def admin_repair_stale_people_memberships():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'REPAIR_STALE_MEMBERSHIPS':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = app._coerce_bool(data.get('dryRun', True))
    queued = app._enqueue_admin_repair_job(user_id, action='repair_stale_memberships', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/backfill/photos', methods=['POST'])
@admin_bp.route('/admin/backfill/photos', methods=['POST'])
def admin_backfill_photos():
    """Re-queue existing photos through the processing pipeline.

    Marks processing steps as 'queued' (force=True) for every non-deleted,
    non-video photo in the user's library. The browser's background scheduler
    picks them up via /upload/processing/pending and re-runs them — identical
    to what happens for a freshly uploaded photo. When PROCESSING_MODE is
    'backend' or 'both', each photo is also enqueued to ipworker
    (_queue_ipwork_processing) -- this is the way to bulk-reprocess an
    existing library server-side without any browser tab needing to be open.

    By default all steps are re-queued (thumbnails, EXIF, OCR, AI vision, map
    tagging, and face detection). Pass a 'steps' list in the body to scope the
    re-queue to a subset, e.g. {"steps": ["ocr"]} to re-run OCR only, across
    every photo in the library.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'BACKFILL_ALL_PHOTOS':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403

    all_steps = ['preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face']
    requested_steps = data.get('steps')
    if requested_steps is None:
        steps_to_run = all_steps
    else:
        if not isinstance(requested_steps, list) or not requested_steps:
            return app.jsonify({'error': 'steps must be a non-empty list', 'code': 'invalid_steps'}), 400
        invalid_steps = [step for step in requested_steps if step not in all_steps]
        if invalid_steps:
            return app.jsonify({'error': f'invalid steps: {invalid_steps}', 'code': 'invalid_steps'}), 400
        steps_to_run = requested_steps

    try:
        metadata_rows = app._cached_metadata_rows_for_user(user_id, purpose='admin.backfill')
    except Exception as exc:
        app.app.logger.exception('Backfill: failed to load metadata for %s', user_id)
        return app.jsonify({'error': 'Failed to load photo metadata'}), 503

    queued = 0
    skipped = 0
    for row in metadata_rows:
        filename = str(row.get('RowKey') or '').strip()
        if not filename:
            continue
        if str(row.get('processing_state') or '').strip().lower() == 'deleted':
            skipped += 1
            continue
        if app.is_video_file(filename):
            skipped += 1
            continue
        try:
            app._enqueue_processing_steps(user_id, filename, steps_to_run, force=True)
            # Bulk/background reprocessing of an already-uploaded library is
            # one of the two reasons ipworker exists (see the ipworker plan) --
            # without this, backend/both mode would only ever reach ipworker
            # for brand-new uploads (_queue_upload_processing), and this
            # admin action would silently do nothing beyond flipping table
            # status columns nothing consumes.
            app._queue_ipwork_processing(user_id, filename, steps=steps_to_run)
            queued += 1
        except Exception:
            app.app.logger.exception('Backfill: failed to enqueue steps for %s/%s', user_id, filename)
            skipped += 1

    app._invalidate_metadata_scan_cache(user_id)
    app.app.logger.info('Backfill queued %d photos (steps=%s), skipped %d for user %s', queued, steps_to_run, skipped, user_id)
    return app.jsonify({
        'queued': queued,
        'skipped': skipped,
        'total': queued + skipped,
        'steps': steps_to_run,
    })

@admin_bp.route('/api/admin/ipwork/enqueue', methods=['POST'])
@admin_bp.route('/admin/ipwork/enqueue', methods=['POST'])
def admin_enqueue_ipwork():
    """Queue specific steps for a caller-supplied set of photos, server-side.

    The Tools page's per-step buttons (Thumbnails/EXIF/OCR/AI vision/Map
    tagging/Faces) used to only call startBrowserProcessing() -- a purely
    client-side pipeline. Under PROCESSING_MODE=backend that pipeline
    self-gates every step to a no-op (see runBrowserProcessing in
    PhotoGallery.tsx), so those buttons silently did nothing. This is the
    selection-scoped counterpart to admin_backfill_photos() above (which
    already does this, but only for the *entire* library): it lets a
    specific set of filenames be (re)enqueued to ipworker without touching
    the rest of the account. No confirm gate is required here, unlike the
    library-wide backfill -- the blast radius is bounded to whatever the
    caller explicitly selected.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}

    all_steps = ['preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face']
    requested_steps = data.get('steps')
    if not isinstance(requested_steps, list) or not requested_steps:
        return app.jsonify({'error': 'steps must be a non-empty list', 'code': 'invalid_steps'}), 400
    invalid_steps = [step for step in requested_steps if step not in all_steps]
    if invalid_steps:
        return app.jsonify({'error': f'invalid steps: {invalid_steps}', 'code': 'invalid_steps'}), 400
    steps_to_run = requested_steps

    raw_filenames = data.get('filenames')
    if not isinstance(raw_filenames, list) or not raw_filenames:
        return app.jsonify({'error': 'filenames must be a non-empty list', 'code': 'invalid_filenames'}), 400
    if len(raw_filenames) > 2000:
        return app.jsonify({'error': 'Too many filenames', 'code': 'too_many_filenames'}), 400
    force = bool(data.get('force'))

    queued = 0
    skipped = 0
    for raw_name in raw_filenames:
        filename = app._validate_media_filename(str(raw_name or ''))
        entity = app._get_metadata_entity(user_id, filename) if filename else None
        if not entity or str(entity.get('processing_state') or '').strip().lower() == 'deleted' or app.is_video_file(filename):
            skipped += 1
            continue
        try:
            app._enqueue_processing_steps(user_id, filename, steps_to_run, force=force)
            # Same reasoning as admin_backfill_photos: without this, backend/both
            # mode would never reach ipworker for an already-uploaded photo the
            # user re-runs from the Tools page.
            app._queue_ipwork_processing(user_id, filename, steps=steps_to_run)
            queued += 1
        except Exception:
            app.app.logger.exception('ipwork enqueue: failed to enqueue steps for %s/%s', user_id, filename)
            skipped += 1

    app.app.logger.info('ipwork enqueue: queued %d photos (steps=%s), skipped %d for user %s', queued, steps_to_run, skipped, user_id)
    return app.jsonify({
        'queued': queued,
        'skipped': skipped,
        'total': queued + skipped,
        'steps': steps_to_run,
    })

@admin_bp.route('/api/admin/photos/purge-orphaned-data', methods=['POST'])
@admin_bp.route('/admin/photos/purge-orphaned-data', methods=['POST'])
def admin_purge_orphaned_photo_data():
    """Purge metadata, face rows, and person records for photos whose image blob
    no longer exists. Uses the images blob container as the source of truth.

    Requires ``repair: true, confirm: 'PURGE_ORPHANED_PHOTO_DATA'`` in the body
    to execute; omit ``confirm`` (or pass ``dryRun: true``) to preview only."""
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    dry_run = app._coerce_bool(data.get('dryRun', True))
    if not dry_run and (
        data.get('repair') is not True
        or data.get('confirm') != 'PURGE_ORPHANED_PHOTO_DATA'
    ):
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    queued = app._enqueue_admin_repair_job(user_id, action='purge_orphaned', dry_run=dry_run)
    return app.jsonify(app._clustering_queue_response(queued))

@admin_bp.route('/api/admin/jobs/status', methods=['GET'])
def admin_job_status():
    """Poll result for a job enqueued by one of the admin repair/rebuild
    routes above (people_admin_repair, vector_index_rebuild) -- these run on
    the standalone clustering worker instead of inline on a backend request
    thread (see _enqueue_admin_repair_job), so the Tools page polls here for
    the same dry-run preview / apply result it used to get synchronously."""
    user_id, error = app._require_user_id()
    if error:
        return error
    job_id = str(app.request.args.get('jobId', '') or '')
    if not job_id or app.metadata_table_client is None:
        return app.jsonify({'status': 'unknown'})
    try:
        row = app.metadata_table_client.get_entity(partition_key='jobs', row_key=app._job_row_key(job_id))
    except Exception:
        return app.jsonify({'status': 'unknown'})
    if str(row.get('userId') or '') != user_id:
        return app.jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = app.json.loads(result)
        except Exception:
            pass
    return app.jsonify({'status': str(row.get('status') or 'unknown'), 'result': result, 'error': row.get('error')})
