"""Blueprint: albums routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

albums_bp = Blueprint('albums', __name__)

@albums_bp.route('/albums/delete-multiple', methods=['POST'])
@albums_bp.route('/api/albums/delete-multiple', methods=['POST'])
def delete_multiple_albums_people():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_feature_available():
        return app.jsonify({'error': 'Albums/people features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    album_ids = data.get('albumIds', [])
    person_ids = data.get('personIds', [])
    if not isinstance(album_ids, list) or not isinstance(person_ids, list):
        return app.jsonify({'error': 'albumIds and personIds must be lists'}), 400

    deleted_albums = []
    album_errors = []
    deleted_persons = []
    person_errors = []
    updated_files = []

    for album_id in album_ids:
        try:
            app.albums_table_client.delete_entity(partition_key=user_id, row_key=str(album_id))
            deleted_albums.append(album_id)
        except Exception as exc:
            app.app.logger.warning('Album delete failed for %s: %s', album_id, exc)
            album_errors.append({'albumId': album_id, 'error': 'delete failed'})

    if person_ids:
        person_set = set(str(pid) for pid in person_ids)
        for pid in list(person_set):
            try:
                app.person_table_client.delete_entity(partition_key=user_id, row_key=pid)
                deleted_persons.append(pid)
            except Exception as exc:
                app.app.logger.warning('Person delete failed for %s: %s', pid, exc)
                person_errors.append({'personId': pid, 'error': 'delete failed'})

        try:
            rows = list(app.metadata_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'"))
        except Exception:
            rows = []

        for row in rows:
            try:
                people_ids = app.json.loads(row.get('peopleIds', '[]') or '[]')
            except Exception:
                people_ids = []
            updated = [pid for pid in people_ids if pid not in person_set]
            if updated != people_ids:
                row['peopleIds'] = app.json.dumps(updated)
                try:
                    app.metadata_table_client.upsert_entity(row)
                    updated_files.append(row.get('RowKey'))
                except Exception:
                    pass

    return app.jsonify({
        'deletedAlbums': deleted_albums,
        'albumErrors': album_errors,
        'deletedPersonIds': deleted_persons,
        'personErrors': person_errors,
        'updatedFiles': updated_files,
        'success': len(album_errors) == 0 and len(person_errors) == 0,
    })

@albums_bp.route('/albums', methods=['GET'])
@albums_bp.route('/api/albums', methods=['GET'])
def list_albums():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    try:
        rows = list(app.albums_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'"))
    except Exception:
        rows = []
    albums = [app._album_entity_to_payload(row) for row in rows]
    return app.jsonify({'albums': albums})

@albums_bp.route('/albums', methods=['POST'])
@albums_bp.route('/api/albums', methods=['POST'])
def create_album():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return app.jsonify({'error': 'Album name is required'}), 400
    album_id = str(app.uuid.uuid4())
    now = app.datetime.now(app.timezone.utc).isoformat()
    entity = {
        'PartitionKey': user_id,
        'RowKey': album_id,
        'name': name,
        'filenames': app.json.dumps([]),
        'createdAt': now,
        'updatedAt': now,
        'isPublic': False,
        'publicToken': '',
        'publicExpiresAt': '',
        'accessCode': '',
    }
    app._save_album_entity(entity)
    return app.jsonify({'album': app._album_entity_to_payload(entity)})

@albums_bp.route('/albums/<album_id>', methods=['GET'])
@albums_bp.route('/api/albums/<album_id>', methods=['GET'])
def get_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404
    payload = app._album_entity_to_payload(entity)
    photos = app._load_photos_for_filenames(user_id, payload.get('filenames', []))
    return app.jsonify({'album': payload, 'photos': photos})

@albums_bp.route('/albums/<album_id>/photos/add', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/photos/add', methods=['POST'])
def add_photos_to_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404
    data = app.request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return app.jsonify({'error': 'filenames must be a list'}), 400

    current = set(app._album_filenames(entity))
    added = []
    errors = []
    for filename in filenames:
        safe = app._validate_media_filename(str(filename))
        if not safe:
            errors.append(f'{filename}: Invalid filename')
            continue
        if not app._get_metadata_entity(user_id, safe):
            errors.append(f'{filename}: Not found')
            continue
        if safe not in current:
            current.add(safe)
            added.append(safe)

    entity['filenames'] = app.json.dumps(list(current))
    entity['updatedAt'] = app.datetime.now(app.timezone.utc).isoformat()
    app._save_album_entity(entity)
    return app.jsonify({'success': True, 'added': added, 'errors': errors, 'album': app._album_entity_to_payload(entity)})

@albums_bp.route('/albums/<album_id>/photos/remove', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/photos/remove', methods=['POST'])
def remove_photos_from_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404
    data = app.request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return app.jsonify({'error': 'filenames must be a list'}), 400

    current = set(app._album_filenames(entity))
    removed = []
    for filename in filenames:
        if filename in current:
            current.remove(filename)
            removed.append(filename)

    entity['filenames'] = app.json.dumps(list(current))
    entity['updatedAt'] = app.datetime.now(app.timezone.utc).isoformat()
    app._save_album_entity(entity)
    return app.jsonify({'success': True, 'removed': removed, 'album': app._album_entity_to_payload(entity)})

@albums_bp.route('/albums/<album_id>/rename', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/rename', methods=['POST'])
def rename_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404
    data = app.request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return app.jsonify({'error': 'Album name is required'}), 400
    entity['name'] = name
    entity['updatedAt'] = app.datetime.now(app.timezone.utc).isoformat()
    app._save_album_entity(entity)
    return app.jsonify({'album': app._album_entity_to_payload(entity)})

@albums_bp.route('/albums/<album_id>/delete', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/delete', methods=['POST'])
def delete_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    try:
        app.albums_table_client.delete_entity(partition_key=user_id, row_key=album_id)
    except Exception as exc:
        app.app.logger.exception('delete_album failed')
        return app.jsonify({'error': 'Internal server error'}), 500
    return app.jsonify({'success': True})

@albums_bp.route('/albums/autocreate', methods=['POST'])
@albums_bp.route('/api/albums/autocreate', methods=['POST'])
def autocreate_albums():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503

    data = app.request.get_json(silent=True) or {}
    requested_rule = str(data.get('rule') or 'recent-upload').strip().lower()
    rule = app.SMART_ALBUM_RULES.get(requested_rule)
    if not rule:
        return app.jsonify({
            'error': 'Invalid smart album rule',
            'rules': sorted(set(app.SMART_ALBUM_RULES.values())),
        }), 400

    try:
        metadata_rows = app._cached_metadata_rows_for_user(user_id, purpose='albums.smart_create')
    except Exception as exc:
        app.app.logger.exception('Smart album metadata read failed')
        return app.jsonify({'error': 'Unable to read photo metadata.'}), 503

    try:
        existing_rows = list(app.albums_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'"))
    except Exception:
        existing_rows = []

    existing_names = {row.get('name') for row in existing_rows if row.get('name')}
    candidates = app._smart_album_candidates(user_id, rule, metadata_rows)

    for candidate in candidates:
        name = candidate.get('name') or ''
        filenames = candidate.get('filenames') or []
        if name in existing_names:
            continue
        album_id = str(app.uuid.uuid4())
        now = app.datetime.now(app.timezone.utc).isoformat()
        entity = {
            'PartitionKey': user_id,
            'RowKey': album_id,
            'name': name,
            'filenames': app.json.dumps(filenames),
            'createdAt': now,
            'updatedAt': now,
            'isPublic': False,
            'publicToken': '',
            'publicExpiresAt': '',
            'accessCode': '',
        }
        app._save_album_entity(entity)
        payload = app._album_entity_to_payload(entity)
        return app.jsonify({
            'count': 1,
            'rule': rule,
            'album': payload,
        })

    return app.jsonify({
        'count': 0,
        'rule': rule,
        'album': None,
        'message': 'No new matching smart album could be created for this rule.',
    })

@albums_bp.route('/albums/<album_id>/share', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/share', methods=['POST'])
def share_album(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404

    data = app.request.get_json(silent=True) or {}
    enabled = app._coerce_bool(data.get('enabled', True))
    expires_in_days = int(data.get('expiresInDays', 0) or 0)
    access_code = (data.get('accessCode') or '').strip()
    clear_access_code = app._coerce_bool(data.get('clearAccessCode', False))
    if access_code and not clear_access_code and len(access_code) < app.MIN_ALBUM_ACCESS_CODE_LENGTH:
        return app.jsonify({'error': f'Access code must be at least {app.MIN_ALBUM_ACCESS_CODE_LENGTH} characters.'}), 400

    entity['isPublic'] = enabled
    if enabled and not entity.get('publicToken'):
        entity['publicToken'] = str(app.uuid.uuid4())
    if not enabled:
        entity['publicToken'] = ''

    if expires_in_days > 0:
        expires = app.datetime.now(app.timezone.utc) + app.timedelta(days=expires_in_days)
        entity['publicExpiresAt'] = expires.isoformat()
    else:
        entity['publicExpiresAt'] = ''

    if clear_access_code:
        entity['accessCode'] = ''
    elif access_code:
        entity['accessCode'] = access_code

    entity['updatedAt'] = app.datetime.now(app.timezone.utc).isoformat()
    app._save_album_entity(entity)
    return app.jsonify({'album': app._album_entity_to_payload(entity)})

@albums_bp.route('/albums/<album_id>/revoke', methods=['POST'])
@albums_bp.route('/api/albums/<album_id>/revoke', methods=['POST'])
def revoke_album_share(album_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._albums_table_available():
        return app.jsonify({'error': 'Albums not configured'}), 503
    entity = app._load_album_entity(user_id, album_id)
    if not entity:
        return app.jsonify({'error': 'Album not found'}), 404

    entity['isPublic'] = False
    entity['publicToken'] = ''
    entity['publicExpiresAt'] = ''
    entity['accessCode'] = ''
    entity['updatedAt'] = app.datetime.now(app.timezone.utc).isoformat()
    app._save_album_entity(entity)
    return app.jsonify({'album': app._album_entity_to_payload(entity)})
