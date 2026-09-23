"""Blueprint: public routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

public_bp = Blueprint('public', __name__)

@public_bp.route('/public/album/<token>', methods=['GET'])
def public_album_share_page(token: str):
    """Crawler-facing share page for a public album.

    This is the URL users actually copy/share (see `_album_entity_to_payload`).
    It lives on the backend (not the SPA's own /public/album/<token> route)
    because the SPA is a static single-page app -- every route serves the same
    static index.html with fixed OG tags, so link-preview bots (iMessage,
    WhatsApp, Slack) never see a given album's real name/photo. This page
    returns real tags for the bot, then meta-refreshes real browsers into the
    interactive SPA viewer.
    """
    entity = app._find_public_album_by_token(token)
    meta = app._public_album_share_meta(entity, token)
    redirect_url = f'{app._get_spa_base_url()}/public/album/{app._urlquote(str(token))}'
    resp = app.make_response(app._render_public_album_share_page(meta, redirect_url))
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@public_bp.route('/public/albums/<token>', methods=['GET'])
@public_bp.route('/public/albums/<token>', methods=['POST'])
@public_bp.route('/api/public/albums/<token>', methods=['GET'])
@public_bp.route('/api/public/albums/<token>', methods=['POST'])
def public_album(token: str):
    entity = app._find_public_album_by_token(token)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404
    if not app._coerce_bool(entity.get('isPublic', False)):
        return app.jsonify({'error': 'Album not public'}), 404
    if app._album_is_expired(entity):
        return app.jsonify({'error': 'Album expired'}), 404

    access_code = app._album_access_code(entity)
    provided = ''
    if app.request.method == 'POST':
        data = app.request.get_json(silent=True) or {}
        provided = (data.get('accessCode') or '').strip()

    gate = app._album_access_code_gate(entity, token, provided)
    if gate is not None:
        return gate

    filenames = app._album_filenames(entity)
    owner_id = str(entity.get('PartitionKey') or '')
    photos = []
    for name in filenames:
        metadata = app._get_metadata_entity(owner_id, name) if owner_id else {}
        if (metadata or {}).get('processing_state') == 'deleted':
            continue
        urls = app._public_photo_urls(token, name, blob_name=app._blob_name_from_metadata(metadata, name))
        photos.append({
            'filename': name,
            'url': urls['url'],
            'thumbnailUrl': urls['thumbnailUrl'],
            'previewUrl': urls.get('previewUrl') or '',
            'rawFullPreviewUrl': urls.get('rawFullPreviewUrl') or '',
            'rotation': app._normalize_rotation((metadata or {}).get('rotation', 0)),
            'thumbnailRotation': app._thumbnail_rotation_from_metadata(metadata),
        })

    resp = app.make_response(app.jsonify({
        'album': {
            'name': entity.get('name', ''),
            'photoCount': len(filenames),
        },
        'photos': photos,
    }))
    # Issue a signed grant so the browser can subsequently load the (code-protected)
    # media, which are fetched as <img src> and cannot carry the access code themselves.
    if access_code:
        resp.set_cookie(
            app._album_grant_cookie_name(token),
            app._sign_album_grant(token, access_code),
            httponly=True,
            secure=app.request.is_secure,
            samesite='Lax',
            max_age=60 * 60 * 6,
            path='/',
        )
    return resp

@public_bp.route('/public/photos/<token>/thumbnail/<path:filename>', methods=['GET'])
def public_thumbnail(token: str, filename: str):
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    if not app._album_grant_valid(entity, token):
        return app.jsonify({'error': 'Not found'}), 404
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in app._album_filenames(entity):
        return app.jsonify({'error': 'Not found'}), 404

    if not app.blob_service_client:
        return app.jsonify({'error': 'Thumbnail service not configured'}), 503

    # Resolve the physical blob (anonymous UUID for anonymized photos) for the
    # album owner. The thumbnail blob shares the image's anonymous id.
    owner_id = str(entity.get('PartitionKey') or '')
    blob_name_to_serve = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name

    try:
        props = app.get_media_properties('thumbnail', blob_name_to_serve)
        content_type = props.get('content_type') or 'image/jpeg'
        return app._stream_media_response(
            'thumbnail',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='public, max-age=3600',
            content_length=props.get('size'),
        )
    except Exception as exc:
        if app._is_missing_media_error(exc):
            resp = app.Response(app.placeholder_bytes, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return resp
        print(f"Unexpected error serving public thumbnail for {safe_name}: {str(exc)}", flush=True)
        return app.jsonify({'error': 'Thumbnail not found'}), 404

@public_bp.route('/public/photos/<token>/image/<path:filename>', methods=['GET'])
def public_image(token: str, filename: str):
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    if not app._album_grant_valid(entity, token):
        return app.jsonify({'error': 'Not found'}), 404
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in app._album_filenames(entity):
        return app.jsonify({'error': 'Not found'}), 404

    owner_id = str(entity.get('PartitionKey') or '')
    blob_name_to_serve = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name

    try:
        try:
            props = app.get_media_properties('image', blob_name_to_serve)
            content_type = props.get('content_type') or 'image/jpeg'
            content_length = props.get('size')
        except Exception:
            content_type = 'image/jpeg'
            content_length = None
        return app._stream_media_response(
            'image',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='public, max-age=3600',
            content_length=content_length,
            download_filename=safe_name,
        )
    except Exception as exc:
        if app._is_missing_media_error(exc):
            return app.jsonify({'error': 'File not found in storage'}), 404
        app.app.logger.exception('Failed to serve public image for %s', safe_name)
        return app.jsonify({'error': 'Failed to retrieve image'}), 500

@public_bp.route('/public/photos/<token>/share-preview/<path:filename>', methods=['GET'])
def public_photo_share_preview(token: str, filename: str):
    """A properly link-preview-sized JPEG for a public album's OG image.

    The original photo (used directly for `url`) can be many MB at full
    sensor resolution -- too large for WhatsApp/Facebook/Slack/Twitter's
    crawler size caps (5-8MB) -- while the 120x120 gallery thumbnail is below
    their ~200x200 minimum and gets silently dropped instead. This reuses
    `convert_image_to_jpeg` (already used by the RAW/HEIC preview route
    above), which bounds to 2048px / ~1MB via `_encode_preview_jpeg` --
    comfortably inside every major platform's limits.
    """
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    if not app._album_grant_valid(entity, token):
        return app.jsonify({'error': 'Not found'}), 404
    safe_name = app._validate_media_filename(filename)
    if not safe_name or safe_name not in app._album_filenames(entity):
        return app.jsonify({'error': 'Not found'}), 404
    try:
        owner_id = str(entity.get('PartitionKey') or '')
        blob_name = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        image_bytes = app.download_media_bytes('image', blob_name)
        preview_bytes = app.convert_image_to_jpeg(image_bytes, safe_name)
        resp = app.Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    except Exception as exc:
        if app._is_missing_media_error(exc):
            return app.jsonify({'error': 'File not found in storage'}), 404
        app.app.logger.exception('Failed to build public share-preview image for %s', safe_name)
        return app.jsonify({'error': 'Failed to build preview'}), 503

@public_bp.route('/public/photos/<token>/preview/<path:filename>', methods=['GET'])
def public_preview(token: str, filename: str):
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    if not app._album_grant_valid(entity, token):
        return app.jsonify({'error': 'Not found'}), 404
    safe_name = app._validate_media_filename(filename)
    if not safe_name:
        return app.jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in app._album_filenames(entity):
        return app.jsonify({'error': 'Not found'}), 404

    if app._filename_requires_backend_preview(safe_name):
        owner_id = str(entity.get('PartitionKey') or '')
        cached_blob_name = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        try:
            cached = app._stream_cached_preview(safe_name, cache_control='public, max-age=3600', blob_name=cached_blob_name)
        except Exception:
            app.app.logger.exception('Failed to stream cached public preview for %s', safe_name)
            cached = None
        if cached is not None:
            return cached
        queued = app._enqueue_preview_generation_job(owner_id, safe_name) if owner_id else {'status': 'failed'}
        if queued.get('status') in {'queued', 'already_queued'}:
            return app.jsonify({
                'error': 'Preview is being prepared',
                'reason': 'preview_queued',
                'detail': 'The server queued a background preview build for this file. Try again shortly.',
            }), 503
        return app.jsonify({
            'error': 'Preview not available yet',
            'reason': 'preview_unavailable',
            'detail': 'Preview generation is unavailable right now. Please try again later.',
        }), 503

    try:
        owner_id = str(entity.get('PartitionKey') or '')
        blob_name_to_read = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        image_bytes = app.download_media_bytes('image', blob_name_to_read)
        preview_bytes = app.convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes or (app._filename_requires_backend_preview(safe_name) and not app._looks_like_jpeg(preview_bytes)):
            return app.jsonify(app._preview_failure_payload(safe_name)), 422
        resp = app.Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    except Exception as exc:
        if app._is_missing_media_error(exc):
            return app.jsonify({'error': 'File not found in storage', 'reason': 'missing'}), 404
        app.app.logger.exception('Failed to create public preview for %s', safe_name)
        return app.jsonify({
            'error': 'Failed to create preview',
            'reason': 'server_error',
            'detail': 'The server hit an error while building this preview.',
        }), 503

@public_bp.route('/public/photos/<token>/raw-full-preview/<path:filename>', methods=['GET'])
def public_raw_full_preview(token: str, filename: str):
    """Public-album equivalent of proxy_raw_full_preview (routes/photos.py)
    for the lightbox's FR ("full resolution") button on RAW photos. Without
    this, the FR button always called the authenticated-only backend route,
    which 401s for anonymous album visitors. Never falls back to a full
    demosaic, same reasoning as the authenticated route.
    """
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    if not app._album_grant_valid(entity, token):
        return app.jsonify({'error': 'Not found'}), 404
    safe_name = app._validate_media_filename(filename)
    if not safe_name or safe_name not in app._album_filenames(entity):
        return app.jsonify({'error': 'Not found'}), 404

    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if ext not in app.RAW_EXTENSIONS_RAWPY and ext not in app.RAW_EXTENSIONS_CINEMA:
        return app.jsonify({'error': 'Not a RAW file'}), 400

    try:
        owner_id = str(entity.get('PartitionKey') or '')
        blob_name_to_read = app.resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        image_bytes = app.download_media_bytes('image', blob_name_to_read)
    except Exception as exc:
        if app._is_missing_media_error(exc):
            return app.jsonify({'error': 'File not found in storage'}), 404
        app.app.logger.exception('Failed to read public RAW original for %s', safe_name)
        return app.jsonify({'error': 'Failed to retrieve image'}), 503

    preview_bytes = app.extract_raw_native_preview_bytes(image_bytes, safe_name)
    if not preview_bytes:
        return app.jsonify({
            'error': 'No native preview available',
            'reason': 'raw_native_preview_unavailable',
            'detail': 'No higher-resolution preview is available for this RAW file — showing the standard preview.',
        }), 404

    resp = app.Response(preview_bytes, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@public_bp.route('/public/albums/<token>/download-check', methods=['GET'])
def public_album_download_check(token: str):
    """Cheap reachability/validity probe the frontend calls before submitting
    the real download form. The form POST (below) can't be driven through
    httpClient — a browser form submission gives no programmatic success/
    failure signal — so this lets a dead backend or an expired/removed album
    be caught and shown to the user instead of silently opening a blank tab.
    Deliberately does not re-validate the access code (the album page load
    already did, and the real download POST re-checks it via the grant
    cookie): this only needs to answer "is there something to download",
    which doesn't require credentialed cross-origin CORS.
    """
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404
    return app.jsonify({'ok': True})

@public_bp.route('/public/albums/<token>/download', methods=['POST'])
def public_album_download(token: str):
    entity = app._find_public_album_by_token(token)
    if not entity or not app._coerce_bool(entity.get('isPublic', False)) or app._album_is_expired(entity):
        return app.jsonify({'error': 'Not found'}), 404

    data = app.request.form.to_dict(flat=True) if app.request.form else (app.request.get_json(silent=True) or {})
    provided = (data.get('accessCode') or '').strip()
    gate = app._album_access_code_gate(entity, token, provided)
    if gate is not None:
        return gate

    raw_filenames = data.get('filenames', [])
    filenames: app.List[str]
    if isinstance(raw_filenames, str) and raw_filenames.strip():
        try:
            parsed = app.json.loads(raw_filenames)
            filenames = [str(item) for item in parsed if isinstance(item, (str, int, float))]
        except Exception:
            filenames = [item.strip() for item in raw_filenames.split(',') if item.strip()]
    elif isinstance(raw_filenames, list):
        filenames = [str(item) for item in raw_filenames]
    else:
        filenames = app._album_filenames(entity)

    album_filenames = set(app._album_filenames(entity))
    selected = [name for name in filenames if name in album_filenames]
    if not selected:
        selected = app._album_filenames(entity)

    # Build the archive on a temp file on disk rather than in a BytesIO. A whole
    # album buffered in RAM (and previously copied a second time for the
    # Response body) could exceed the container's memory limit and OOM-kill the
    # replica. Spooling to disk keeps peak memory to roughly one photo at a time
    # (the bytes returned by download_media_bytes), and the response is streamed
    # straight off disk, then the temp file is removed once the stream drains.
    tmp = app.tempfile.NamedTemporaryFile(prefix='album-', suffix='.zip', delete=False)
    tmp_path = tmp.name
    written_count = 0
    try:
        owner_id = str(entity.get('PartitionKey') or '')
        with app.zipfile.ZipFile(tmp, 'w', compression=app.zipfile.ZIP_DEFLATED) as zip_file:
            for name in selected:
                try:
                    # Read from the physical (anonymous) blob, but keep the original
                    # filename as the entry name so users get familiar names.
                    blob_name = app.resolve_physical_blob_name(owner_id, name, 'image') if owner_id else name
                    data_bytes = app.download_media_bytes('image', blob_name)
                    zip_file.writestr(name, data_bytes)
                    written_count += 1
                except Exception as exc:
                    print(f"Skipping {name} while creating public album download: {str(exc)}", flush=True)
        tmp.close()
    except Exception:
        try:
            tmp.close()
        finally:
            app._remove_file_quietly(tmp_path)
        raise

    if written_count == 0:
        app._remove_file_quietly(tmp_path)
        return app.jsonify({'error': 'No files could be downloaded'}), 404

    zip_size = app.os.path.getsize(tmp_path)

    def _stream_and_cleanup():
        try:
            with open(tmp_path, 'rb') as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            app._remove_file_quietly(tmp_path)

    resp = app.Response(app.stream_with_context(_stream_and_cleanup()), mimetype='application/zip')
    resp.headers['Content-Length'] = str(zip_size)
    resp.headers['Content-Disposition'] = f'attachment; filename=public-album-{token}.zip'
    resp.headers['Cache-Control'] = 'no-store'
    return resp
