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

def _refuse_wave_owned_reprocess(user_id):
    """A wave-owned library runs all processing through upload waves. The legacy
    re-queue routes below would supersede a photo's generation (resetting its
    steps to pending) and then queue nothing, stranding it, so refuse up front,
    before any row is touched."""
    if app._library_cut_over_to_waves(user_id):
        return app.jsonify({
            'error': 'This library processes photos through upload waves; re-queueing existing photos is not supported.',
            'code': 'wave_owned_library',
        }), 409
    return None


@admin_bp.route('/api/admin/indexes/source-reservations', methods=['POST'])
def recover_source_reservations():
    library, error = app._require_admin(require_auth=True)
    if error:
        return error
    operator, _library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    import automatic_index_updates as automatic
    from index_artifact_cleanup import artifact_catalog_serialization
    data = app.request.get_json(silent=True) or {}
    action = data.get('action')
    try:
        if action == 'fence':
            result = automatic.operator_fence(library, operator=operator, reason=data.get('reason'))
        else:
            fence = data.get('fenceId')
            if not isinstance(fence, str) or not fence:
                raise ValueError('fenceId required')
            with artifact_catalog_serialization(library, storage=app.storage_utils_module) as guard:
                if action == 'prove':
                    if data.get('confirm') != 'ALL_LIBRARY_WRITERS_STOPPED':
                        raise ValueError('ALL_LIBRARY_WRITERS_STOPPED confirmation required')
                    result = automatic.operator_prove(library, fence, guard=guard, operator=operator,
                        reason=data.get('reason'), all_writers_stopped=data.get('allWritersStopped'),
                        no_paused_requests=data.get('noPausedRequests'),
                        legacy_writers_stopped=data.get('legacyWritersStopped'))
                elif action == 'recover':
                    result = automatic.recover_quiesced_source_reservations(library, guard=guard, fence_id=fence)
                elif action == 'resume':
                    result = automatic.resume_quiesced_sources(library, guard=guard, fence_id=fence)
                else:
                    raise ValueError('Unknown recovery action')
        return app.jsonify(result)
    except ValueError as exc:
        return app.jsonify({'error': str(exc)}), 400
    except Exception as exc:
        app.worker_logger.warning('Quiesced index recovery deferred', exc_info=True)
        return app.jsonify({'error': str(exc), 'status': 'deferred'}), 409

@admin_bp.route('/api/admin/people/dedupe-faces', methods=['POST'])
@admin_bp.route('/admin/people/dedupe-faces', methods=['POST'])
def admin_dedupe_faces():
    user_id, error = app._require_library_owner_id()
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
    user_id, error = app._require_library_owner_id()
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
    user_id, error = app._require_library_owner_id()
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
    user_id, error = app._require_library_owner_id()
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
        user_id, error = app._require_library_owner_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        data = app.request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'REBUILD_VECTOR_INDEX':
            return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        status = app.enqueue_index_build(user_id, reason='manual-vector-repair', scope='vector', coalesce=False)
        accepted = status in ('queued', 'already_active')
        return app.jsonify({
            'success': accepted, 'queued': accepted, 'status': status,
            'jobId': app._index_build_job_id(user_id) + '-vector' if accepted else None,
        }), 202 if accepted else 503
    except Exception as exc:
        app.app.logger.exception('Admin vector index rebuild enqueue failed')
        return app.jsonify({'error': 'Admin vector index rebuild failed'}), 500

@admin_bp.route('/api/admin/people/repair-stale-memberships', methods=['POST'])
@admin_bp.route('/admin/people/repair-stale-memberships', methods=['POST'])
def admin_repair_stale_people_memberships():
    user_id, error = app._require_library_owner_id()
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

    Marks processing steps as 'queued' (force=True) for each non-deleted,
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

    Each request visits at most 10 metadata rows. Repeat with the returned
    continuation cursor until complete=True; never hold an HTTP request open
    while enqueueing an entire library. Table rows are ordered by RowKey
    within the user's partition, so a strict keyset cursor avoids rescanning.
    """
    user_id, error = app._require_library_owner_id()
    if error:
        return error
    refused = _refuse_wave_owned_reprocess(user_id)
    if refused:
        return refused
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
        steps_to_run = list(dict.fromkeys(requested_steps))

    cursor = data.get('continuation')
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024):
        return app.jsonify({'error': 'invalid continuation cursor', 'code': 'invalid_continuation'}), 400
    if app.PROCESSING_MODE != 'browser' and app.ipwork_queue_client is None:
        return app.jsonify({'error': 'Backend processing queue is unavailable', 'code': 'ipwork_unavailable'}), 503

    # Streamed with a two-column projection: this only needs each filename and
    # whether it is trashed, never the whole library in memory.
    queued = 0
    skipped = 0
    failed = 0
    visited = 0
    continuation = None
    complete = True
    try:
        metadata_rows = app._iter_metadata_rows_for_user(
            user_id, select=['RowKey', 'processing_state'], include_deleted=True, purpose='admin.backfill',
            extra_filter=f"RowKey gt '{app._escape_odata(cursor)}'" if cursor else '',
            page_size=11,
        )
        for row in metadata_rows:
            if visited >= 10:
                complete = False
                break
            visited += 1
            # Keep the exact key for pagination, including whitespace.
            filename = str(row.get('RowKey') or '')
            continuation = filename
            if not filename or str(row.get('processing_state') or '').strip().lower() == 'deleted' or app.is_video_file(filename):
                skipped += 1
                continue
            try:
                results = app._enqueue_processing_steps(user_id, filename, steps_to_run, force=True)
                if not results or any(result.get('status') != 'queued' for result in results.values()):
                    raise RuntimeError('Failed to reset processing steps')
                queue_result = app._queue_ipwork_processing(user_id, filename, steps=steps_to_run)
                expected_status = 'skipped' if app.PROCESSING_MODE == 'browser' else 'queued'
                if queue_result.get('status') != expected_status:
                    raise RuntimeError(f"Processing queue returned {queue_result.get('status')}")
                queued += 1
            except Exception:
                app.app.logger.exception('Backfill: failed to enqueue steps for %s/%s', user_id, filename)
                failed += 1
    except Exception:
        # The table iterator is lazy: query/page errors occur during iteration,
        # not when the generator is constructed. Preserve partial counts.
        app.app.logger.exception('Backfill: failed to load metadata for %s', user_id)
        return app.jsonify({'error': 'Failed to load photo metadata', 'queued': queued,
                            'skipped': skipped, 'failed': failed, 'complete': False}), 503

    app._invalidate_metadata_scan_cache(user_id)
    app.app.logger.info('Backfill batch queued %d photos (steps=%s), skipped %d, failed %d, complete=%s for user %s',
                        queued, steps_to_run, skipped, failed, complete, user_id)
    return app.jsonify({
        'queued': queued,
        'skipped': skipped,
        'failed': failed,
        'total': queued + skipped + failed,
        'steps': steps_to_run,
        'continuation': None if complete else continuation,
        'complete': complete,
        'processingMode': app.PROCESSING_MODE,
    })

def _uploads_open(user_id) -> bool:
    """True while an upload session is feeding a wave in this wave-owned library."""
    if not app._library_cut_over_to_waves(user_id):
        return False
    from upload_waves import ShadowWaves
    return bool((ShadowWaves(app.jobs_table_client, user_id)._get('control') or {}).get('collectingWaveId'))


@admin_bp.route('/api/admin/backfill/grid-images', methods=['POST'])
@admin_bp.route('/admin/backfill/grid-images', methods=['POST'])
def admin_backfill_grid_images():
    """Queue sharp grid-tile images (grid_images.py) for every photo in the
    library that has a preview but no grid image yet.

    Runs on the clustering worker, not in this request: batches of
    grid_images.BATCH_SIZE photos, each queueing its own continuation. It reads
    previews only, never originals, and skips photos that already have a grid
    image, so it is safe to repeat. Refused while uploads are open (pass
    "force": true to override a stale upload); photos still being processed
    are skipped either way and picked up by their wave's own grid job.
    """
    user_id, error = app._require_library_owner_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'BACKFILL_GRID_IMAGES':
        return app.jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    if not app.grid_images.enabled():
        return app.jsonify({'error': 'Grid images are disabled', 'code': 'grid_images_disabled'}), 503
    if data.get('force') is not True and _uploads_open(user_id):
        return app.jsonify({'error': 'Photos are uploading; run the backfill once uploads finish.',
                            'code': 'upload_in_progress'}), 409
    queued = app._enqueue_grid_images(user_id, reason='admin_backfill')
    if not queued:
        return app.jsonify({'error': 'Background queue is unavailable', 'code': 'queue_unavailable'}), 503
    return app.jsonify({'queued': True, 'batchSize': app.grid_images.BATCH_SIZE}), 202


@admin_bp.route('/api/admin/people/verify-face-index', methods=['POST'])
@admin_bp.route('/admin/people/verify-face-index', methods=['POST'])
def admin_verify_face_index():
    """Force a real re-scan of specific filenames' face lookup rows and report
    any mismatch against what they claimed before -- a bounded, caller-
    selected safety net for the keyed photofacebyfilename index, not a
    library-wide action. No confirm gate: read-mostly, and any correction it
    makes is strictly fixing the lookup to match what the face table already
    says, the same thing the normal amortized path does for unknown rows.
    """
    user_id, error = app._require_library_owner_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    raw_filenames = data.get('filenames')
    if not isinstance(raw_filenames, list) or not raw_filenames:
        return app.jsonify({'error': 'filenames must be a non-empty list', 'code': 'invalid_filenames'}), 400
    if len(raw_filenames) > 500:
        return app.jsonify({'error': 'Too many filenames (max 500)', 'code': 'too_many_filenames'}), 400
    filenames = []
    for raw_name in raw_filenames:
        filename = app._validate_media_filename(str(raw_name or ''))
        if filename:
            filenames.append(filename)
    if not filenames:
        return app.jsonify({'error': 'No valid filenames provided', 'code': 'invalid_filenames'}), 400
    result = app._verify_face_filename_indexes(user_id, filenames)
    return app.jsonify(result)

@admin_bp.route('/api/admin/photos/purge-orphaned-data', methods=['POST'])
@admin_bp.route('/admin/photos/purge-orphaned-data', methods=['POST'])
def admin_purge_orphaned_photo_data():
    """Purge metadata, face rows, and person records for photos whose image blob
    no longer exists. Uses the images blob container as the source of truth.

    Requires ``repair: true, confirm: 'PURGE_ORPHANED_PHOTO_DATA'`` in the body
    to execute; omit ``confirm`` (or pass ``dryRun: true``) to preview only."""
    user_id, error = app._require_library_owner_id()
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
