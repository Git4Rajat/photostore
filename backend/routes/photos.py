"""Blueprint: photos routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint

import app

photos_bp = Blueprint('photos', __name__)

@photos_bp.route('/api/photos/thumbnail/<path:filename>', methods=['GET'])
def proxy_thumbnail(filename: str):
    """Serve a thumbnail blob or a placeholder when the blob is missing."""
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400

    metadata_entity = app._get_metadata_entity(user_id, safe_name)
    if not metadata_entity:
        return app.jsonify({'error': 'Not found'}), 404

    # Resolve the blob name: use anonymous ID if available, fallback to original filename
    blob_name_to_serve = app._resolve_media_blob_name(user_id, safe_name, metadata_entity)

    try:
        props = app.get_media_properties('thumbnail', blob_name_to_serve)
        content_type = props.get('content_type') or 'image/jpeg'
        return app._stream_media_response(
            'thumbnail',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
        )
    except Exception as e:
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            if app._filename_requires_backend_preview(safe_name):
                return proxy_preview(safe_name)
            resp = app.Response(app.placeholder_bytes, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return resp
        print(f"Unexpected error serving thumbnail for {safe_name}: {str(e)}", flush=True)
        return app.jsonify({'error': 'Failed to access thumbnail'}), 503

@photos_bp.route('/api/photos/access/<kind>/<path:filename>', methods=['GET'])
def photo_access_url(kind: str, filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400
    metadata = app._get_metadata_entity(user_id, safe_name)
    if not metadata:
        return app.jsonify({'error': 'Not found'}), 404
    if not app._is_supported_photo_access_kind(kind):
        return app.jsonify({'error': 'Invalid media kind'}), 400
    if not app.blob_service_client or not app.account_name:
        return app.jsonify({'error': 'Media access is not configured'}), 503
    if kind == 'preview':
        fallback = app._preview_access_response(safe_name, metadata)
        if fallback is not None:
            return app.jsonify(fallback)
    if kind == 'thumbnail':
        fallback = app._thumbnail_access_response(safe_name, metadata)
        if fallback is not None:
            return app.jsonify(fallback)
    container = app._photo_access_container(kind)
    if container is None:
        return app.jsonify({'error': 'Invalid media kind'}), 400
    try:
        blob_name = app._blob_name_from_metadata(metadata, safe_name)
        if kind == 'preview':
            blob_name = app._preview_cache_blob_name(blob_name)
        url, expires_at = app._create_stable_read_sas_url(
            container,
            blob_name,
            download_filename=safe_name if kind == 'image' else None,
        )
        return app.jsonify(app._access_url_response(url, expires_at, safe_name, kind))
    except Exception as exc:
        app.app.logger.exception('Failed to mint %s access URL for %s', kind, safe_name)
        return app.jsonify({'error': f'Failed to create {kind} access URL'}), 503

@photos_bp.route('/api/photos/access-batch', methods=['POST'])
def photo_access_url_batch():
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    kind = str(data.get('kind') or 'thumbnail').strip().lower()
    filenames = data.get('filenames') or []
    if not app._is_supported_photo_access_kind(kind):
        return app.jsonify({'error': 'Invalid media kind'}), 400
    if not isinstance(filenames, list) or not filenames:
        return app.jsonify({'error': 'filenames must be a non-empty list'}), 400
    if len(filenames) > 2000:
        return app.jsonify({'error': 'Too many filenames'}), 400
    if not app.blob_service_client or not app.account_name:
        return app.jsonify({'error': 'Media access is not configured'}), 503

    # Resolve metadata from the same cached full-account scan /photos uses,
    # instead of one Table Storage get_entity round trip per filename. That
    # per-filename loop was the dominant cost of this endpoint -- it scales
    # directly with batch size, and a zoomed-out gallery page can request
    # 100+ filenames in one call (see pageSizeForZoomLevel), turning into
    # 100+ sequential round trips (tens of seconds). The scan is normally
    # already warm here: /photos populates it moments earlier for the same
    # page, on the same 20s TTL (_metadata_scan_cache).
    try:
        cached_rows = app._cached_metadata_list_rows_for_user(user_id, purpose='photos.access_batch')
        metadata_map = {row['RowKey']: row for row in cached_rows if row.get('RowKey')}
    except Exception:
        metadata_map = {}

    safe_names = []
    for raw_name in filenames:
        safe_name = app._validate_media_filename(str(raw_name or ''))
        if safe_name:
            safe_names.append(safe_name)

    # _cached_metadata_list_rows_for_user deliberately excludes trashed rows
    # (processing_state == 'deleted'), so every filename on the Recently
    # Deleted page misses the cache here -- not just brand-new uploads. A
    # serial per-filename _get_metadata_entity fallback turned that page's
    # batch (up to 200 filenames) into 200 sequential Table Storage round
    # trips, slow enough that the whole request could fail and leave every
    # tile showing the empty-thumbnail placeholder. Fan the misses out
    # concurrently instead, same bounded-concurrency pattern the delete
    # endpoint above already uses for per-file Table Storage I/O.
    misses = [name for name in safe_names if name not in metadata_map]
    if misses:
        with ThreadPoolExecutor(max_workers=app.DELETE_IO_CONCURRENCY) as executor:
            fetched = executor.map(lambda name: (name, app._get_metadata_entity(user_id, name)), misses)
        for name, entity in fetched:
            if entity:
                metadata_map[name] = entity

    urls: app.Dict[str, str] = {}
    expires_at = ''
    for safe_name in safe_names:
        metadata = metadata_map.get(safe_name)
        if not metadata:
            continue
        container = app._photo_access_container(kind)
        if container is None:
            continue
        if kind == 'thumbnail':
            fallback = app._thumbnail_access_response(safe_name, metadata)
            if fallback is not None:
                urls[safe_name] = fallback['url']
                continue
        if kind == 'preview':
            fallback = app._preview_access_response(safe_name, metadata)
            if fallback is not None:
                urls[safe_name] = fallback['url']
                continue
        try:
            blob_name = app._blob_name_from_metadata(metadata, safe_name)
            if kind == 'preview':
                blob_name = app._preview_cache_blob_name(blob_name)
            url, expires_at = app._create_stable_read_sas_url(
                container,
                blob_name,
                download_filename=safe_name if kind == 'image' else None,
            )
            urls[safe_name] = url
        except Exception:
            continue

    return app.jsonify({
        'kind': kind,
        'expiresAt': expires_at,
        'urls': urls,
    })

@photos_bp.route('/api/photos/preview/<path:filename>', methods=['GET'])
def proxy_preview(filename: str):
    """Serve a browser-displayable preview for files that cannot be shown directly."""
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400

    preview_metadata = app._get_metadata_entity(user_id, safe_name)
    if not preview_metadata:
        return app.jsonify({'error': 'Not found'}), 404
    preview_blob_name = app._blob_name_from_metadata(preview_metadata, safe_name)

    # Used to be gated to _filename_requires_backend_preview(safe_name) (RAW/
    # HEIC/JXL only, formats the browser can't render directly at all). Now
    # that the shrunk preview is the default lightbox image for every photo,
    # the cached-blob-or-enqueue-and-503 branch below needs to cover every
    # image file, not just the browser-unviewable ones -- otherwise an
    # ordinary JPEG falls into the synchronous, uncached convert-on-every-
    # request branch further down, which was only ever meant as a rare
    # defensive fallback, not a path fit to serve every photo view.
    # _filename_requires_backend_preview itself is left as-is: it still means
    # exactly what it says ("browser can't display the original directly")
    # and is used elsewhere for that narrower question.
    if not app.is_video_file(safe_name):
        try:
            cached = app._stream_cached_preview(safe_name, cache_control='private, max-age=3600', blob_name=preview_blob_name)
        except Exception:
            app.app.logger.exception('Failed to stream cached preview for %s', safe_name)
            cached = None
        if cached is not None:
            return cached
        queued = app._enqueue_preview_generation_job(user_id, safe_name)
        if queued.get('status') in {'queued', 'already_queued'}:
            return app.jsonify({
                'error': 'Preview is being prepared',
                'reason': 'preview_queued',
                'detail': 'The server queued a background preview build for this file. Try again shortly.',
                'jobId': queued.get('jobId') or '',
                'canDownloadOriginal': True,
            }), 503
        if queued.get('status') == 'unavailable':
            return app.jsonify({
                'error': 'Preview worker unavailable',
                'reason': 'preview_worker_unavailable',
                'detail': 'Preview generation worker is unavailable. Please try again later.',
                'canDownloadOriginal': True,
            }), 503
        return app.jsonify({
            'error': 'Preview queue failed',
            'reason': 'preview_queue_failed',
            'detail': 'Could not queue preview generation. Please try again.',
            'canDownloadOriginal': True,
        }), 503

    try:
        image_bytes = app.download_media_bytes('image', preview_blob_name)
        preview_bytes = app.convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes or (app._filename_requires_backend_preview(safe_name) and not app._looks_like_jpeg(preview_bytes)):
            return app.jsonify(app._preview_failure_payload(safe_name)), 422
        resp = app.Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as exc:
        if app._is_missing_media_error(exc):
            return app.jsonify({
                'error': 'File not found in storage',
                'reason': 'missing',
                'detail': 'The original file could not be found in storage.',
            }), 404
        app.app.logger.exception('Failed to create preview for %s', safe_name)
        return app.jsonify({
            'error': 'Failed to create preview',
            'reason': 'server_error',
            'detail': 'The server hit an error while building this preview. Please try again.',
            'canDownloadOriginal': True,
        }), 503

@photos_bp.route('/api/photos/image/<path:filename>', methods=['GET'])
def proxy_image(filename: str):
    """Serve full image bytes from storage via backend proxy."""
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400

    metadata_entity = app._get_metadata_entity(user_id, safe_name)
    if not metadata_entity:
        return app.jsonify({'error': 'Not found'}), 404

    if not app.blob_service_client:
        return app.jsonify({'error': 'Image service not configured'}), 503

    # Resolve the blob name: use anonymous ID if available, fallback to original filename
    blob_name_to_serve = app._resolve_media_blob_name(user_id, safe_name, metadata_entity)

    try:
        try:
            props = app.get_media_properties('image', blob_name_to_serve)
            content_type = props.get('content_type') or 'image/jpeg'
        except Exception as e:
            # File doesn't exist or can't be accessed
            if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
                return app.jsonify({'error': 'File not found in storage'}), 404
            return app.jsonify({'error': 'Failed to access image metadata'}), 503

        return app._stream_media_response(
            'image',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
            download_filename=safe_name,
        )
    except Exception as e:
        # Check if it's a file not found error
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            return app.jsonify({'error': 'File not found in storage'}), 404
        # Other errors
        print(f"Unexpected error serving image for {safe_name}: {str(e)}", flush=True)
        return app.jsonify({'error': 'Failed to retrieve image'}), 503

@photos_bp.route('/api/photos/raw-full-preview/<path:filename>', methods=['GET'])
def proxy_raw_full_preview(filename: str):
    """Serve the largest embedded RAW preview at native size for the lightbox's
    FR ("full resolution") button.

    Deliberately never falls back to a full demosaic (rawpy.postprocess()) --
    that call has no timeout or resource guard anywhere in this codebase and is
    only safe today because it's confined to the async preview-generation
    worker job, not a synchronous web request. A RAW file with no embedded
    preview simply has no native preview available here.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400

    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if ext not in app.RAW_EXTENSIONS_RAWPY and ext not in app.RAW_EXTENSIONS_CINEMA:
        return app.jsonify({'error': 'Not a RAW file'}), 400

    metadata_entity = app._get_metadata_entity(user_id, safe_name)
    if not metadata_entity:
        return app.jsonify({'error': 'Not found'}), 404

    if not app.blob_service_client:
        return app.jsonify({'error': 'Image service not configured'}), 503

    blob_name_to_serve = app._resolve_media_blob_name(user_id, safe_name, metadata_entity)

    try:
        image_bytes = app.download_media_bytes('image', blob_name_to_serve)
    except Exception as e:
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            return app.jsonify({'error': 'File not found in storage'}), 404
        print(f"Unexpected error reading RAW original for {safe_name}: {str(e)}", flush=True)
        return app.jsonify({'error': 'Failed to retrieve image'}), 503

    preview_bytes = app.extract_raw_native_preview_bytes(image_bytes, safe_name)
    if not preview_bytes:
        return app.jsonify({
            'error': 'No native preview available',
            'reason': 'raw_native_preview_unavailable',
            'detail': 'No higher-resolution preview is available for this RAW file — showing the standard preview.',
        }), 404

    response = app.Response(preview_bytes, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'private, max-age=3600'
    return response

@photos_bp.route('/api/photos/cover/<path:filename>', methods=['GET'])
def proxy_cover(filename: str):
    """Serve a face cover crop from the 'cover' container.

    Cover blobs are named '<sha256(user_id)[:16]>/<face_id>.jpg' (see face_crop),
    so the filename here is a two-segment blob path, not a photo filename. We
    validate the user-hash prefix against the caller so covers can't be read
    across accounts, then stream the bytes.
    """
    user_id, error = app._require_user_id()
    if error:
        return error

    parts = filename.split('/')
    if len(parts) != 2:
        return app.jsonify({'error': 'Invalid cover path'}), 400
    user_hash, leaf = parts
    expected_hash = app.hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:16]
    if user_hash != expected_hash:
        return app.jsonify({'error': 'Not found'}), 404
    if not app._is_safe_path_segment(leaf):
        return app.jsonify({'error': 'Invalid cover path'}), 400
    safe_leaf = leaf

    cover_blob = f'{user_hash}/{safe_leaf}'
    try:
        props = app.get_media_properties('cover', cover_blob)
        content_type = props.get('content_type') or 'image/jpeg'
        return app._stream_media_response(
            'cover',
            cover_blob,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
        )
    except Exception as e:
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            return app.jsonify({'error': 'File not found in storage'}), 404
        print(f"Unexpected error serving cover for {cover_blob}: {str(e)}", flush=True)
        return app.jsonify({'error': 'Failed to retrieve cover'}), 503

@photos_bp.route('/photos', methods=['GET'])
@photos_bp.route('/photos/', methods=['GET'])
@photos_bp.route('/api/photos', methods=['GET'])
@photos_bp.route('/api/photos/', methods=['GET'])
def list_photos():
    try:
        # Default to capture-date order so a request without an explicit sort opens
        # on the most recently taken photos (matches the gallery's default view).
        sort = app.request.args.get('sort', 'capture')
        offset = int(app.request.args.get('offset', '0'))
        limit = int(app.request.args.get('limit', '24'))
    except ValueError:
        return app.jsonify({'error': 'Invalid paging parameters.'}), 400

    capture_start, capture_end = app._parse_capture_range_args()

    user_id, error = app._require_user_id()
    if error:
        return error
    try:
        metadata_rows = app._cached_metadata_list_rows_for_user(user_id, purpose='photos.list')
        entries = [row['RowKey'] for row in metadata_rows if row.get('RowKey')]
        metadata_map = {row['RowKey']: row for row in metadata_rows if row.get('RowKey')}
    except Exception as exc:
        app.app.logger.exception('Photo list metadata read failed')
        return app.jsonify({'error': 'Unable to read photo metadata.'}), 503

    # Backfill: rows uploaded before finalize persisted uploadDate sort via the
    # volatile last_processing_update fallback. Stamp the derived value as their
    # permanent uploadDate (best-effort, capped per request) so their position
    # can never shift again — e.g. when a legacy photo gets reprocessed.
    backfilled = 0
    for name in entries:
        if backfilled >= app.UPLOAD_DATE_BACKFILL_MAX_PER_REQUEST:
            break
        row = metadata_map.get(name) or {}
        if row.get('uploadDate'):
            continue
        derived = str(row.get('upload_started_at') or row.get('last_processing_update') or '')
        if not derived:
            continue
        try:
            app._update_metadata_entity_fields(user_id, name, {'uploadDate': derived})
            row['uploadDate'] = derived
            backfilled += 1
        except Exception:
            break  # storage hiccup: stop backfilling, listing still works

    # Deterministic ordering with a filename tie-break so the gallery returns an
    # identical sequence on every load (see ordering_utils.order_photo_entries).
    entries = app.order_photo_entries(entries, metadata_map, sort)

    if capture_start or capture_end:
        entries = [name for name in entries if app._capture_in_range(metadata_map.get(name, {}), capture_start, capture_end)]

    selected = entries[offset:offset + limit]

    # Persist blob size for legacy rows that predate finalize-time stamping, so the
    # gallery stops doing a blob HEAD per tile. Capped per request (converges over
    # a few page views); after that _build_photo_summary reads size from metadata
    # with head_missing=False and never HEADs.
    props_backfilled = 0
    for name in selected:
        if props_backfilled >= app.PHOTO_PROPS_BACKFILL_MAX_PER_REQUEST:
            break
        row = metadata_map.get(name) or {}
        if row.get('size'):
            continue
        try:
            props = app.get_media_properties('image', app._blob_name_from_metadata(row, name))
        except Exception:
            break  # storage hiccup: stop backfilling, listing still works
        size_val = int(props.get('size') or 0)
        if not size_val:
            continue
        updates: app.Dict[str, object] = {'size': size_val}
        lm = props.get('last_modified')
        if lm is not None:
            updates['lastModified'] = lm.isoformat()
        try:
            app._update_metadata_entity_fields(user_id, name, updates)
            row.update(updates)
            props_backfilled += 1
        except Exception:
            break

    pid_to_name, _ = app._load_people_name_index(user_id)
    photos = app._build_photo_summaries_page(
        user_id,
        [(filename, metadata_map.get(filename, {})) for filename in selected],
        pid_to_name,
    )

    return app.jsonify({'photos': photos, 'total': len(entries)})

@photos_bp.route('/photos/processing-status', methods=['GET'])
@photos_bp.route('/photos/processing-status/', methods=['GET'])
@photos_bp.route('/api/photos/processing-status', methods=['GET'])
@photos_bp.route('/api/photos/processing-status/', methods=['GET'])
def photos_processing_status():
    """Point-lookup refresh for the gallery's processing-status poller
    (PhotoGallery.tsx) -- lets the "processing on server" tile icon update
    without re-fetching/relisting the whole page. Bounded to a small explicit
    filename list, not a scan."""
    user_id, error = app._require_user_id()
    if error:
        return error
    raw_filenames = app.request.args.get('filenames', '')
    filenames = [
        f for f in (app._validate_media_filename(name.strip()) for name in raw_filenames.split(',') if name.strip()) if f
    ][:100]
    statuses: app.Dict[str, app.Dict] = {}
    for filename in filenames:
        entity = app._get_metadata_entity(user_id, filename)
        if entity is None or entity.get('processing_state') == 'deleted':
            continue
        statuses[filename] = {
            'preview': entity.get('preview_status'),
            'thumbnail': entity.get('thumbnail_status'),
            'exif': entity.get('exif_status'),
            'ocr': entity.get('ocr_status'),
            'face': entity.get('face_status'),
            'aiVision': entity.get('ai_vision_status'),
            'mapDetection': entity.get('map_detection_status'),
            'activeWorker': app._active_processing_worker(entity),
        }
    return app.jsonify({'statuses': statuses})

@photos_bp.route('/photos/lookup/<path:filename>', methods=['GET'])
@photos_bp.route('/photos/lookup/<path:filename>/', methods=['GET'])
@photos_bp.route('/api/photos/lookup/<path:filename>', methods=['GET'])
@photos_bp.route('/api/photos/lookup/<path:filename>/', methods=['GET'])
def lookup_photo(filename: str):
    # Exact-filename point lookup for deep links (e.g. "view in library" from a
    # page that doesn't otherwise share the gallery's paginated listing), so the
    # caller doesn't need to guess a page offset or rely on fuzzy search ranking.
    user_id, error = app._require_user_id()
    if error:
        return error
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400
    metadata = app._get_metadata_entity(user_id, safe_name)
    if not metadata or metadata.get('processing_state') == 'deleted':
        return app.jsonify({'error': 'Not found'}), 404
    pid_to_name, _ = app._load_people_name_index(user_id)
    return app.jsonify({'photo': app._build_photo_summary(user_id, safe_name, metadata, include_props=False, pid_to_name=pid_to_name)})

@photos_bp.route('/photos/lookup-batch', methods=['POST'])
@photos_bp.route('/api/photos/lookup-batch', methods=['POST'])
def lookup_photos_batch():
    # Batched counterpart to lookup_photo, for deep links that need to pull
    # in several specific photos at once (e.g. "open N selected photos in
    # Workbench") without N round trips.
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    raw = data.get('filenames')
    if not isinstance(raw, list) or not raw:
        return app.jsonify({'error': 'filenames must be a non-empty list', 'code': 'invalid_filenames'}), 400
    if len(raw) > 200:
        return app.jsonify({'error': 'Too many filenames', 'code': 'too_many_filenames'}), 400
    pid_to_name, _ = app._load_people_name_index(user_id)
    photos = []
    for raw_name in raw:
        safe_name = app._validate_media_filename(str(raw_name or ''))
        if not safe_name:
            continue
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata or metadata.get('processing_state') == 'deleted':
            continue
        photos.append(app._build_photo_summary(user_id, safe_name, metadata, include_props=False, pid_to_name=pid_to_name))
    return app.jsonify({'photos': photos})

@photos_bp.route('/photos/timeline', methods=['GET'])
@photos_bp.route('/photos/timeline/', methods=['GET'])
@photos_bp.route('/api/photos/timeline', methods=['GET'])
@photos_bp.route('/api/photos/timeline/', methods=['GET'])
def photos_timeline():
    # Single compact year/month/day summary for the client-side zoomable
    # timeline (see timeline_metadata.build_timeline_summary). Reuses the same
    # cached full-partition scan as /photos, so a newly-uploaded photo appears
    # here within the same staleness window (METADATA_SCAN_CACHE_TTL_SECONDS)
    # it appears in the gallery, with no separate cache/invalidation to manage.
    user_id, error = app._require_user_id()
    if error:
        return error
    try:
        metadata_rows = app._cached_metadata_list_rows_for_user(user_id, purpose='photos.timeline')
    except Exception as exc:
        app.app.logger.exception('Timeline metadata read failed')
        return app.jsonify({'error': 'Unable to read photo metadata.'}), 503
    return app.jsonify(app.build_timeline_summary(metadata_rows))

@photos_bp.route('/photos/search', methods=['GET'])
@photos_bp.route('/photos/search/', methods=['GET'])
@photos_bp.route('/api/photos/search', methods=['GET'])
@photos_bp.route('/api/photos/search/', methods=['GET'])
def search_photos():
    query = (app.request.args.get('q') or '').strip()
    if not query:
        return app.jsonify({'photos': [], 'total': 0})

    try:
        offset = int(app.request.args.get('offset', '0'))
        limit = int(app.request.args.get('limit', '24'))
    except ValueError:
        return app.jsonify({'error': 'Invalid paging parameters.'}), 400

    capture_start, capture_end = app._parse_capture_range_args()
    # Bare year in the query text ("beach 2022") narrows to that calendar year,
    # same as if the user had set the explicit date-range control -- only when
    # they didn't already set one, so it never overrides a real choice. This is
    # deliberately just a year: month/season/"last summer"-style parsing is a
    # bigger, separate feature, not a one-line regex.
    matched_year = None
    if capture_start is None and capture_end is None:
        year_match = app.re.search(r'\b(19|20)\d{2}\b', query)
        if year_match:
            matched_year = int(year_match.group(0))
            capture_start = app.datetime(matched_year, 1, 1, tzinfo=app.timezone.utc)
            capture_end = app.datetime(matched_year, 12, 31, tzinfo=app.timezone.utc)

    user_id, error = app._require_user_id()
    if error:
        return error

    rows = None
    try:
        lexical_index = app.get_user_lexical_index(user_id, allow_refresh=True)
        if lexical_index is not None:
            rows = lexical_index.get('rows')
    except Exception as exc:
        app.app.logger.warning('Lexical index unavailable for user=%s, falling back to full scan: %s', user_id, exc)
        rows = None

    if rows is None:
        try:
            rows = app._cached_metadata_rows_for_user(user_id, purpose='photos.search')
        except Exception as exc:
            app.app.logger.exception('Photo search metadata read failed')
            return app.jsonify({'error': 'Unable to read photo metadata.'}), 503

    pid_to_name, name_to_ids = app._load_people_name_index(user_id)
    matched_person_groups = app._matched_query_people_groups(query, name_to_ids)
    matched_location_terms = app._matched_query_locations(query, rows)
    tokens = app.parse_search_query(query)
    app._expand_tokens_with_tag_embeddings(tokens, user_id)
    query_embedding = app.vision_utils.encode_text_embedding(app.build_expanded_query_text(query, tokens))
    current_embedding_version = app.vision_utils.get_text_embedding_version()
    vector_scores: app.Dict[str, float] = {}
    if query_embedding:
        for row_key, score in app.vector_search_candidates(user_id, query_embedding, top_k=max(limit * 25, 500), allow_refresh=False):
            if row_key:
                vector_scores[row_key] = score
    semantic_threshold = float(app.os.getenv('SEMANTIC_SEARCH_THRESHOLD', '0.16'))
    has_context_intent = bool(tokens.get('required_object') and tokens.get('modifiers'))
    scored: app.List[app.Tuple[float, str, app.Dict]] = []
    fallback_scored: app.List[app.Tuple[float, str, app.Dict]] = []

    for row in rows:
        filename = row.get('RowKey')
        if not filename:
            continue
        row = app._metadata_with_people_names(row, pid_to_name)

        # Tier 1: hard filters. A row failing any of these is excluded
        # unconditionally, before scoring ever runs.
        if not app._row_passes_search_filters(row, capture_start, capture_end, matched_person_groups, matched_location_terms):
            continue

        # Tier 2: scoring. Always computes both lexical and semantic signals
        # in full -- neither can veto the other.
        exif_data = app.parse_exif_data(row.get('exifData', '{}'))
        score, lexical_score, semantic_text = app._score_search_row(
            user_id, tokens, filename, row, exif_data,
            query_embedding=query_embedding,
            vector_scores=vector_scores,
            current_embedding_version=current_embedding_version,
            semantic_threshold=semantic_threshold,
            matched_person_groups=matched_person_groups,
            matched_location_terms=matched_location_terms,
        )
        if score <= 0:
            continue

        # Tier 3: bucketing/ranking. Every row reaching here already has a
        # positive combined score; this only decides primary vs. fallback.
        if app._search_row_belongs_in_fallback_bucket(
            score, lexical_score, semantic_text, tokens, filename, row,
            has_context_intent=has_context_intent,
        ):
            fallback_scored.append((score, filename, row))
        else:
            scored.append((score, filename, row))

    fallback_notice = None
    if has_context_intent and not scored and fallback_scored:
        modifier = tokens.get('modifiers', [''])[0]
        obj = tokens.get('required_object', [''])[0]
        fallback_notice = f"No {modifier} {obj} found. Showing {obj} results instead."
        scored = fallback_scored

    scored.sort(key=lambda item: item[0], reverse=True)
    total = len(scored)
    selected = scored[offset:offset + limit]

    photos = app._build_photo_summaries_page(
        user_id,
        [(filename, metadata) for _, filename, metadata in selected],
        pid_to_name,
    )

    response_payload = {'photos': photos, 'total': total}
    if fallback_notice:
        response_payload['searchNotice'] = fallback_notice
    # Surfaces why results matched (person/location chips in the UI) --
    # already computed above for filtering/scoring, just wasn't returned.
    if matched_person_groups:
        matched_people = sorted({
            pid_to_name[group[0]] for group in matched_person_groups if group and pid_to_name.get(group[0])
        })
        if matched_people:
            response_payload['matchedPeople'] = matched_people
        # Per-person match counts for the Ask results' People panel -- counted
        # over the full ranked/scored set (pre-pagination), not just the
        # current page, so the count reflects the whole result set.
        people_detail = []
        for group in matched_person_groups:
            if not group:
                continue
            person_id = group[0]
            name = pid_to_name.get(person_id)
            if not name:
                continue
            group_ids = set(group)
            count = 0
            for _, _, row in scored:
                try:
                    row_people_ids = set(app.json.loads(row.get('peopleIds', '[]') or '[]'))
                except Exception:
                    row_people_ids = set()
                if row_people_ids & group_ids:
                    count += 1
            people_detail.append({'personId': person_id, 'name': name, 'count': count})
        if people_detail:
            people_detail.sort(key=lambda item: item['count'], reverse=True)
            response_payload['matchedPeopleDetail'] = people_detail
    if matched_location_terms:
        response_payload['matchedLocations'] = [app._smart_album_title(term) for term in matched_location_terms]
    if matched_year:
        response_payload['matchedYear'] = matched_year
    return app.jsonify(response_payload)

@photos_bp.route('/api/photos/search-index', methods=['GET'])
def photos_search_index():
    # Hands the browser direct SAS URLs to the per-user lexical-index blob
    # (also used server-side by get_user_lexical_index) and vector-index
    # blob, plus the small people-name index -- see
    # localSearchIndex.ts/localLexicalSearch.ts/localVectorIndexParser.ts on
    # the frontend, which run parse_search_query/lexical_search_score's
    # exact logic plus a real client-side CLIP text-query encode (Phase B of
    # backend-cpu-optimization-2026-09) so most /photos/search traffic never
    # has to reach the backend at all. The blobs' own bytes stream straight
    # from storage, never through this request.
    #
    # Lexical index: ensured fresh via allow_refresh=True (safe -- built from
    # plain metadata fields, no ML/version dependency). Vector index:
    # deliberately NOT read via get_user_vector_index -- see
    # get_vector_index_manifest_summary's docstring for why that function's
    # version-gated freshness check is both always-false and actively
    # destructive to call from this torch-less role. This just hands out
    # whatever real index already exists (built by ipworker/the browser),
    # best-effort, without ever touching that gate.
    user_id, error = app._require_user_id()
    if error:
        return error
    try:
        lexical_index = app.get_user_lexical_index(user_id, allow_refresh=True)
    except Exception:
        lexical_index = None
    if lexical_index is None:
        return app.jsonify({'available': False}), 503
    try:
        container_name, blob_name = app.get_lexical_index_blob_location(user_id)
        index_url, expires_at = app._create_stable_read_sas_url(container_name, blob_name)
    except Exception:
        app.app.logger.exception('Failed to mint lexical index SAS URL for %s', user_id)
        return app.jsonify({'available': False}), 503

    vector_index_payload = None
    try:
        vector_manifest = app.get_vector_index_manifest_summary(user_id)
        if vector_manifest and not vector_manifest.get('dirty'):
            vcontainer_name, vblob_name = app.get_vector_index_blob_location(user_id)
            vector_index_url, vector_expires_at = app._create_stable_read_sas_url(vcontainer_name, vblob_name)
            vector_index_payload = {
                'vectorIndexUrl': vector_index_url,
                'vectorIndexExpiresAt': vector_expires_at,
                'embeddingVersion': vector_manifest.get('embedding_version'),
            }
    except Exception:
        app.app.logger.warning('Vector index unavailable for %s, semantic search stays lexical-only', user_id)
        vector_index_payload = None

    pid_to_name, name_to_ids = app._load_people_name_index(user_id)
    response_payload = {
        'available': True,
        'indexUrl': index_url,
        'expiresAt': expires_at,
        'sourceVersion': lexical_index.get('source_version'),
        'updatedAt': lexical_index.get('updated_at'),
        'peopleNameIndex': {'pidToName': pid_to_name, 'nameToIds': name_to_ids},
    }
    if vector_index_payload:
        response_payload.update(vector_index_payload)
    return app.jsonify(response_payload)

@photos_bp.route('/photos/metadata', methods=['POST'])
@photos_bp.route('/photos/metadata/', methods=['POST'])
@photos_bp.route('/api/photos/metadata', methods=['POST'])
@photos_bp.route('/api/photos/metadata/', methods=['POST'])
def photos_metadata():
    user_id, error = app._require_user_id()
    if error:
        return error

    data = app.request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return app.jsonify({'error': 'Invalid request'}), 400

    metadata = {}
    for filename in filenames:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            metadata[filename] = {'error': 'Invalid filename'}
            continue

        row = app._get_metadata_entity(user_id, safe_name)
        if not row:
            metadata[filename] = {'error': 'Not found'}
            continue

        try:
            props = app.get_media_properties('image', app._blob_name_from_metadata(row, safe_name))
            metadata[filename] = {
                'size': props.get('size'),
                'lastModified': props.get('last_modified').isoformat() if props.get('last_modified') else None,
            }
        except Exception:
            metadata[filename] = {'error': 'Not found'}

    return app.jsonify(metadata)

def _parse_filenames_request():
    """Shared body parsing for the trash-family endpoints: {filenames: [...]}
    -> (valid_names, errors) with each name run through _validate_media_filename
    and de-duped, same contract delete_multiple_photos always used."""
    data = app.request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list) or len(filenames) == 0:
        return [], ['Invalid request']
    valid_names = []
    errors = []
    seen = set()
    for filename in filenames:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            errors.append(f'{filename}: Invalid filename')
            continue
        if safe_name in seen:
            continue
        seen.add(safe_name)
        valid_names.append(safe_name)
    return valid_names, errors


@photos_bp.route('/photos/delete', methods=['POST'])
@photos_bp.route('/photos/delete/', methods=['POST'])
@photos_bp.route('/api/photos/delete', methods=['POST'])
@photos_bp.route('/api/photos/delete/', methods=['POST'])
def delete_multiple_photos():
    """Soft-delete: moves photos to trash (processing_state='deleted' +
    deletedAt stamped) instead of removing them outright. Blob, faces, album
    membership, and job rows are left untouched so restore is a pure flag
    flip -- see app._mark_processing_deleted_for_file. The real, irreversible
    delete now lives at /photos/trash/purge (and the retention sweep), both
    backed by app._hard_delete_photos_now, which still does everything this
    endpoint used to do directly."""
    user_id, error = app._require_user_id()
    if error:
        return error

    valid_names, errors = _parse_filenames_request()
    deleted = []
    if not valid_names:
        return app.jsonify({'deleted': deleted, 'errors': errors, 'success': False})

    names_set = set(valid_names)

    # Point-reads only (no partition scan) -- see _hard_delete_photos_now's
    # sibling comment for why that mattered on large accounts.
    with ThreadPoolExecutor(max_workers=app.DELETE_IO_CONCURRENCY) as executor:
        metadata_results = list(executor.map(lambda n: (n, app._get_metadata_entity(user_id, n)), valid_names))
    own_rows_by_name = {name: metadata for name, metadata in metadata_results if metadata is not None}
    temp_removed_names = app._batch_delete_upload_temp_files(names_set)

    def _soft_delete_one_file(safe_name: str) -> app.Tuple[str, str, str]:
        """Returns (safe_name, outcome, detail); outcome one of 'deleted',
        'not_found', 'error'. A photo with no metadata row yet (still
        mid-upload) has nothing to trash -- clearing its temp file, same as
        before, is as far as "delete" goes for it."""
        if safe_name not in own_rows_by_name:
            return (safe_name, 'deleted', '') if safe_name in temp_removed_names else (safe_name, 'not_found', '')
        entity = app._mark_processing_deleted_for_file(user_id, safe_name)
        if entity is None:
            return safe_name, 'error', 'metadata: soft-delete failed'
        return safe_name, 'deleted', ''

    with ThreadPoolExecutor(max_workers=app.DELETE_IO_CONCURRENCY) as executor:
        file_results = list(executor.map(_soft_delete_one_file, valid_names))

    for safe_name, outcome, detail in file_results:
        if outcome == 'deleted':
            deleted.append(safe_name)
        elif outcome == 'not_found':
            errors.append(f'{safe_name}: Not found')
        else:
            errors.append(f'{safe_name}: {detail}')

    if deleted:
        app._invalidate_metadata_scan_cache(user_id)
        try:
            app.touch_user_search_indexes_state(user_id, filenames=deleted)
        except Exception:
            pass

    return app.jsonify({'deleted': deleted, 'errors': errors, 'success': len(deleted) > 0})


@photos_bp.route('/photos/trash', methods=['GET'])
@photos_bp.route('/api/photos/trash', methods=['GET'])
def list_trashed_photos():
    user_id, error = app._require_user_id()
    if error:
        return error

    try:
        offset = max(0, int(app.request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(200, int(app.request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50

    rows = app._query_metadata_rows_for_user(user_id, include_deleted=True, purpose='photos.list_trash')
    trashed = [row for row in rows if row.get('processing_state') == 'deleted']
    trashed.sort(key=lambda row: str(row.get('deletedAt') or ''), reverse=True)

    retention_days = app.TRASH_RETENTION_DAYS
    page = trashed[offset:offset + limit]
    pid_to_name, _ = app._load_people_name_index(user_id)
    photos = app._build_photo_summaries_page(
        user_id,
        [(str(row.get('RowKey') or ''), row) for row in page],
        pid_to_name,
    )
    for photo, row in zip(photos, page):
        deleted_at = str(row.get('deletedAt') or '')
        photo['deletedAt'] = deleted_at
        photo['purgeAt'] = app._compute_trash_purge_at(deleted_at, retention_days)

    response_payload = {'photos': photos, 'total': len(trashed), 'offset': offset, 'limit': limit, 'retentionDays': retention_days}
    if trashed:
        # `trashed` is already sorted by deletedAt descending (most recent
        # first), so the oldest deletion -- the one closest to purging -- is
        # the last row. Free to compute: the full list is already in memory
        # for `total` above, no extra scan for the Activity drawer's summary
        # strip ("Recently Deleted -- N photos, purges in M days").
        response_payload['earliestPurgeAt'] = app._compute_trash_purge_at(str(trashed[-1].get('deletedAt') or ''), retention_days)

    return app.jsonify(response_payload)


@photos_bp.route('/photos/trash/restore', methods=['POST'])
@photos_bp.route('/api/photos/trash/restore', methods=['POST'])
def restore_trashed_photos():
    user_id, error = app._require_user_id()
    if error:
        return error

    valid_names, errors = _parse_filenames_request()
    restored = []
    if not valid_names:
        return app.jsonify({'restored': restored, 'errors': errors, 'success': False})

    for safe_name in valid_names:
        entity = app._restore_deleted_file(user_id, safe_name)
        if entity is None:
            errors.append(f'{safe_name}: Not found')
        else:
            restored.append(safe_name)

    if restored:
        app._invalidate_metadata_scan_cache(user_id)
        try:
            app.touch_user_search_indexes_state(user_id, filenames=restored)
        except Exception:
            pass

    return app.jsonify({'restored': restored, 'errors': errors, 'success': len(restored) > 0})


@photos_bp.route('/photos/trash/restore-all', methods=['POST'])
@photos_bp.route('/api/photos/trash/restore-all', methods=['POST'])
def restore_all_trashed_photos():
    """The Activity drawer's "Restore all" strip action -- every currently-
    trashed photo, without the caller needing to already know filenames
    (unlike /photos/trash/restore, which the RecentlyDeletedPage selection
    flow uses)."""
    user_id, error = app._require_user_id()
    if error:
        return error

    rows = app._query_metadata_rows_for_user(user_id, include_deleted=True, purpose='photos.restore_all_trash')
    trashed_names = [str(row.get('RowKey') or '') for row in rows if row.get('processing_state') == 'deleted']
    trashed_names = [name for name in trashed_names if name]

    restored = []
    errors: app.List[str] = []
    for safe_name in trashed_names:
        entity = app._restore_deleted_file(user_id, safe_name)
        if entity is None:
            errors.append(f'{safe_name}: Not found')
        else:
            restored.append(safe_name)

    if restored:
        app._invalidate_metadata_scan_cache(user_id)
        try:
            app.touch_user_search_indexes_state(user_id, filenames=restored)
        except Exception:
            pass

    return app.jsonify({'restored': restored, 'errors': errors, 'success': len(restored) > 0})


@photos_bp.route('/photos/trash/purge', methods=['POST'])
@photos_bp.route('/api/photos/trash/purge', methods=['POST'])
def purge_trashed_photos():
    """Delete forever: only ever intended for filenames already sitting in
    trash, but doesn't strictly require it -- same irreversible cascade the
    old /photos/delete always ran."""
    user_id, error = app._require_user_id()
    if error:
        return error

    valid_names, errors = _parse_filenames_request()
    if not valid_names:
        return app.jsonify({'deleted': [], 'errors': errors, 'success': False})

    deleted, hard_errors = app._hard_delete_photos_now(user_id, valid_names)
    errors.extend(hard_errors)
    return app.jsonify({'deleted': deleted, 'errors': errors, 'success': len(deleted) > 0})

@photos_bp.route('/photos/<filename>/rating', methods=['POST'])
@photos_bp.route('/photos/<filename>/rating/', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/rating', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/rating/', methods=['POST'])
def set_photo_rating(filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    rating = data.get('rating', 0)

    if not isinstance(rating, int) or rating < 0 or rating > 5:
        return app.jsonify({'error': 'Rating must be between 0 and 5'}), 400

    try:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            return app.jsonify({'error': 'Invalid filename'}), 400
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata:
            return app.jsonify({'error': 'Not found'}), 404
        app._update_metadata_entity_fields(user_id, safe_name, {'rating': rating})
        return app.jsonify({'success': True, 'filename': filename, 'rating': rating})
    except Exception as e:
        app.app.logger.exception('set_photo_rating failed')
        return app.jsonify({'error': 'Internal server error'}), 500

@photos_bp.route('/photos/rate-multiple', methods=['POST'])
@photos_bp.route('/photos/rate-multiple/', methods=['POST'])
@photos_bp.route('/api/photos/rate-multiple', methods=['POST'])
@photos_bp.route('/api/photos/rate-multiple/', methods=['POST'])
def rate_multiple_photos():
    """Bulk rating for the Select command bar's "Rate" action -- same
    {filenames: [...]} body shape as /photos/delete, plus a single shared
    rating applied to every valid photo."""
    user_id, error = app._require_user_id()
    if error:
        return error

    data = app.request.get_json(silent=True) or {}
    rating = data.get('rating', 0)
    if not isinstance(rating, int) or rating < 0 or rating > 5:
        return app.jsonify({'error': 'Rating must be between 0 and 5'}), 400

    valid_names, errors = _parse_filenames_request()
    rated = []
    if not valid_names:
        return app.jsonify({'rated': rated, 'errors': errors, 'success': False})

    def _rate_one(safe_name: str) -> app.Tuple[str, bool]:
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata:
            return safe_name, False
        app._update_metadata_entity_fields(user_id, safe_name, {'rating': rating})
        return safe_name, True

    with ThreadPoolExecutor(max_workers=app.DELETE_IO_CONCURRENCY) as executor:
        results = list(executor.map(_rate_one, valid_names))

    for safe_name, ok in results:
        if ok:
            rated.append(safe_name)
        else:
            errors.append(f'{safe_name}: Not found')

    return app.jsonify({'rated': rated, 'rating': rating, 'errors': errors, 'success': len(rated) > 0})

@photos_bp.route('/photos/<filename>/like', methods=['POST'])
@photos_bp.route('/photos/<filename>/like/', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/like', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/like/', methods=['POST'])
def toggle_like_photo(filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error

    try:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            return app.jsonify({'error': 'Invalid filename'}), 400
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata:
            return app.jsonify({'error': 'Not found'}), 404
        liked_by = app.json.loads(metadata.get('likedBy', '[]'))

        if user_id in liked_by:
            liked_by.remove(user_id)
        else:
            liked_by.append(user_id)

        app._update_metadata_entity_fields(user_id, safe_name, {
            'likes': len(liked_by),
            'likedBy': app.json.dumps(liked_by),
        })

        return app.jsonify({
            'success': True,
            'filename': filename,
            # len(liked_by) is the post-toggle count; metadata['likes'] held the
            # pre-update value (and raised KeyError → 500 on rows without it).
            'likes': len(liked_by),
            'liked': user_id in liked_by,
        })
    except Exception as e:
        app.app.logger.exception('toggle_like_photo failed')
        return app.jsonify({'error': 'Internal server error'}), 500

@photos_bp.route('/photos/<filename>/rotation', methods=['POST'])
@photos_bp.route('/photos/<filename>/rotation/', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/rotation', methods=['POST'])
@photos_bp.route('/api/photos/<filename>/rotation/', methods=['POST'])
def set_photo_rotation(filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    rotation = app._normalize_rotation(data.get('rotation', 0))

    try:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            return app.jsonify({'error': 'Invalid filename'}), 400
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata:
            return app.jsonify({'error': 'Not found'}), 404
        previous_rotation = app._normalize_rotation(metadata.get('rotation', 0))
        updates = {'rotation': rotation}
        if rotation != previous_rotation:
            updates['thumbnail_status'] = 'pending'
        app._update_metadata_entity_fields(user_id, safe_name, {
            **updates,
        })
        return app.jsonify({'success': True, 'filename': filename, 'rotation': rotation})
    except Exception as e:
        app.app.logger.exception('set_photo_rotation failed')
        return app.jsonify({'error': 'Internal server error'}), 500

@photos_bp.route('/photos/<filename>/metadata', methods=['GET'])
@photos_bp.route('/photos/<filename>/metadata/', methods=['GET'])
@photos_bp.route('/api/photos/<filename>/metadata', methods=['GET'])
@photos_bp.route('/api/photos/<filename>/metadata/', methods=['GET'])
def get_photo_metadata(filename: str):
    user_id, error = app._require_user_id()
    if error:
        return error

    try:
        safe_name = app._validate_media_filename(filename)
        if not safe_name:
            return app.jsonify({'error': 'Invalid filename'}), 400
        metadata = app._get_metadata_entity(user_id, safe_name)
        if not metadata:
            return app.jsonify({'error': 'Not found'}), 404
        liked_by = app.json.loads(metadata.get('likedBy', '[]'))
        exif_data = app.parse_exif_data(metadata.get('exifData', '{}'))
        resolution = app._resolution_from_exif(exif_data)
        if not resolution['width'] or not resolution['height']:
            # Not every camera/re-encoder writes EXIF dimension tags. This
            # endpoint is only called once per photo when the info panel is
            # opened (not in bulk listing), so a lazy header-only image read
            # is an acceptable fallback cost here where it wouldn't be in the
            # main photo list endpoint.
            try:
                image_bytes = app.download_media_bytes('image', app._blob_name_from_metadata(metadata, safe_name))
                with app.Image.open(app.io.BytesIO(image_bytes)) as img:
                    resolution = {'width': img.width, 'height': img.height}
            except Exception:
                pass
        return app.jsonify({
            'filename': filename,
            'rating': metadata.get('rating', 0),
            'likes': metadata.get('likes', 0),
            'liked': user_id in liked_by,
            'tags': app.json.loads(metadata.get('tags', '[]')),
            'rotation': app._normalize_rotation(metadata.get('rotation', 0)),
            'objects': app.parse_json_list(metadata.get('objects', '[]')),
            'ocrText': metadata.get('ocrText', ''),
            'caption': metadata.get('caption', ''),
            'exifData': exif_data,
            'exifSummary': app.exif_summary(exif_data) if exif_data else {},
            'resolution': resolution,
            'faces': app.json.loads(metadata.get('faces', '[]') or '[]'),
            'faceCount': metadata.get('faceCount', 0),
            'peopleIds': app.json.loads(metadata.get('peopleIds', '[]') or '[]'),
            'location': app._location_from_metadata(metadata, exif_data),
            'uploadDate': metadata.get('uploadDate'),
        })
    except Exception as e:
        app.app.logger.exception('get_photo_metadata failed')
        return app.jsonify({'error': 'Internal server error'}), 500

@photos_bp.route('/photos/filter', methods=['GET'])
@photos_bp.route('/photos/filter/', methods=['GET'])
@photos_bp.route('/api/photos/filter', methods=['GET'])
@photos_bp.route('/api/photos/filter/', methods=['GET'])
def filter_photos():
    user_id, error = app._require_user_id()
    if error:
        return error

    try:
        min_rating = int(app.request.args.get('minRating', 0))
        min_likes = int(app.request.args.get('minLikes', 0))
        latitude = app.request.args.get('latitude', '')
        longitude = app.request.args.get('longitude', '')
        radius_km = float(app.request.args.get('radius', 0))
        offset = int(app.request.args.get('offset', 0))
        limit = int(app.request.args.get('limit', 24))
    except ValueError:
        return app.jsonify({'error': 'Invalid filter parameters'}), 400

    capture_start, capture_end = app._parse_capture_range_args()

    try:
        # Already sorted (rating/likes -> recency -> filename, stable across
        # loads) -- see _cached_sorted_metadata_rows_for_user. Filtering below
        # preserves that order, so no per-request re-sort is needed.
        all_photos = app._cached_sorted_metadata_list_rows_for_user(user_id, purpose='photos.filter')
    except Exception as exc:
        app.app.logger.exception('Photo filter metadata read failed')
        return app.jsonify({'error': 'Unable to read photo metadata.'}), 503

    try:
        filtered = []

        for photo in all_photos:
            if photo.get('rating', 0) < min_rating:
                continue
            if photo.get('likes', 0) < min_likes:
                continue

            if capture_start or capture_end:
                if not app._capture_in_range(photo, capture_start, capture_end):
                    continue

            if latitude and longitude:
                try:
                    photo_lat = float(photo.get('latitude', 0))
                    photo_lon = float(photo.get('longitude', 0))
                    user_lat = float(latitude)
                    user_lon = float(longitude)
                    distance = ((photo_lat - user_lat) ** 2 + (photo_lon - user_lon) ** 2) ** 0.5
                    if distance > radius_km * 0.01:
                        continue
                except Exception:
                    pass

            filtered.append(photo)

        selected = filtered[offset:offset + limit]
        pid_to_name, _ = app._load_people_name_index(user_id)
        photos = app._build_photo_summaries_page(
            user_id,
            [(photo['RowKey'], photo) for photo in selected],
            pid_to_name,
        )

        return app.jsonify({'photos': photos, 'total': len(filtered), 'offset': offset, 'limit': limit})
    except Exception as e:
        app.app.logger.exception('filter_photos failed')
        return app.jsonify({'error': 'Internal server error'}), 500
