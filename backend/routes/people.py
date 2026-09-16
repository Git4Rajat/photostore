"""Blueprint: people routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

people_bp = Blueprint('people', __name__)

@people_bp.route('/api/persons/cluster', methods=['POST'])
def trigger_clustering():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        data = app.request.get_json(silent=True) or {}
        eps, min_samples = app._resolve_people_cluster_job_params(data.get('eps', app.PEOPLE_CLUSTER_EPS), data.get('minSamples', 2))
        queued = app._enqueue_clustering_job(
            user_id,
            job_type='people_cluster',
            payload={'eps': eps, 'minSamples': min_samples},
        )
        response = app._clustering_queue_response(queued, eps=eps, minSamples=min_samples)
        if queued.get('status') == 'unavailable':
            return app.jsonify(response), 503
        if queued.get('status') == 'failed':
            return app.jsonify(response), 500
        return app.jsonify(response)
    except Exception as exc:
        app.app.logger.exception('People clustering endpoint failed')
        return app.jsonify({'error': 'People clustering failed'}), 500

@people_bp.route('/api/persons', methods=['GET'])
def list_persons():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        q = (app.request.args.get('q') or '').strip().lower()
        names_only = (app.request.args.get('namesOnly') or '').strip().lower() in ('1', 'true')
        try:
            offset = int(app.request.args.get('offset', '0'))
            limit = int(app.request.args.get('limit', '15'))
        except ValueError:
            return app.jsonify({'error': 'Invalid paging parameters.'}), 400

        rows, face_by_id = app._scan_person_and_face_rows(user_id)
        rows_by_id = {str(row.get('RowKey') or ''): row for row in rows}

        # Phase A: one cheap pass over every person using only the bulk face map
        # (no per-face network calls, no SAS minting, no writes) to work out name,
        # named-first order, and total count. The expensive per-person work below
        # (Phase B: full-fidelity face lookups + SAS thumbnail mint) only runs for
        # the requested page slice -- this is what keeps a page load fast
        # regardless of how many clusters/faces the account has.
        entries = []
        unnamed_counter = 1
        for row in rows:
            try:
                person_id = str(row.get('RowKey') or '')
                if not person_id:
                    continue
                try:
                    face_ids = app.json.loads(row.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                active_count = 0
                # See the identical comment in the Phase B loop below for what
                # "indeterminate" protects against. Here, a bulk-map miss is
                # conservatively treated as indeterminate rather than resolved
                # with an individual lookup -- Phase B does that resolution, only
                # for persons that make the page.
                indeterminate = False
                for fid in face_ids:
                    face = face_by_id.get(str(fid))
                    if face is None:
                        indeterminate = True
                        continue
                    if app._face_is_rejected(face) or not app._face_is_owned_by_person(face, person_id):
                        continue
                    active_count += 1

                # See the matching comment in Phase B: never auto-delete a
                # cluster the user explicitly named, even when it's empty.
                is_named = app._person_entity_is_named(row)
                if active_count == 0 and not indeterminate and not is_named:
                    # Eligible for the empty-unnamed auto-delete; Phase B performs
                    # the authoritative check (and the delete) only if this page
                    # is actually requested.
                    continue

                raw_name = str(row.get('name', '') or '').strip()
                name = raw_name
                if not name:
                    name = f'Unnamed {unnamed_counter}'
                    unnamed_counter += 1
                if q and q not in name.lower():
                    continue
                entries.append({'personId': person_id, 'name': name, 'isNamed': is_named, 'faceCount': active_count})
            except Exception:
                continue

        # Stable sort: named clusters first, ties preserve the RowKey order
        # already established above (mirrors the frontend's previous client-side
        # named-first re-sort, now done once here instead).
        entries.sort(key=lambda e: 0 if e['isNamed'] else 1)
        total = len(entries)

        if names_only:
            return app.jsonify({
                'persons': [
                    {'personId': e['personId'], 'name': e['name'], 'faceCount': e['faceCount']}
                    for e in entries
                ],
                'total': total,
            })

        persons = []
        for entry in entries[offset:offset + limit]:
            try:
                person_id = entry['personId']
                row = rows_by_id.get(person_id)
                if row is None:
                    continue
                try:
                    face_ids = app.json.loads(row.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                active_face_ids = []
                rep_face = None
                rep_face_score = None
                # Track whether any face's status could not be determined (a
                # transient lookup error, as opposed to a face that is definitely
                # rejected/reassigned/deleted). We only auto-remove a cluster when
                # every face was positively determined inactive, so a storage blip
                # can never delete a still-valid person.
                indeterminate = False
                for rep_face_id in face_ids:
                    face = face_by_id.get(str(rep_face_id))
                    if face is None:
                        # Not in the bulk summary: look it up so we can tell a
                        # deleted face (definitively inactive) apart from a
                        # transient error (status unknown -> keep the person).
                        if app.face_table_client is None:
                            indeterminate = True
                            continue
                        try:
                            face = app.face_table_client.get_entity(partition_key=user_id, row_key=rep_face_id)
                        except Exception as exc:
                            if not app._is_not_found_error(exc):
                                indeterminate = True
                            continue
                    if app._face_is_rejected(face) or not app._face_is_owned_by_person(face, person_id):
                        continue
                    active_face_ids.append(rep_face_id)
                    score = app._face_preview_priority(face)
                    if rep_face is None or rep_face_score is None or score > rep_face_score:
                        rep_face = app._face_summary_for_person_list(rep_face_id, face, user_id)
                        rep_face_score = score

                # Auto-remove empty clusters so they stop cluttering the People
                # page — BUT never auto-delete a cluster the user explicitly named.
                # Faces can leave a person temporarily (a merge or identity
                # propagation reassigning them); silently deleting a *named* person
                # on a plain list call is data loss — it's how named clusters
                # "vanished after a refresh", and why re-labelling one then 404s.
                # Keep it, show it empty, and let the user delete it explicitly.
                if not active_face_ids and not indeterminate and not entry['isNamed']:
                    try:
                        app.person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                        app.app.logger.info('Removed empty person cluster %s/%s', user_id, person_id)
                    except Exception:
                        pass
                    continue

                persons.append({
                    'personId': person_id,
                    'name': entry['name'],
                    'faceIds': active_face_ids,
                    'faceCount': len(active_face_ids),
                    'representativeFace': rep_face,
                })
            except Exception:
                continue
        return app.jsonify({'persons': persons, 'total': total})
    except Exception as exc:
        app.app.logger.exception('List persons endpoint failed')
        return app.jsonify({'error': 'List persons failed'}), 500

@people_bp.route('/api/persons/<person_id>', methods=['GET'])
def get_person(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    try:
        person = app.person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return app.jsonify({'error': 'Not found'}), 404

    name = str(person.get('name', '') or '').strip()
    if not name:
        name = app._next_unnamed_person_name(user_id)
        person['name'] = name
        try:
            app.person_table_client.upsert_entity(person)
        except Exception:
            pass

    try:
        face_ids = app.json.loads(person.get('faceIds', '[]'))
    except Exception:
        face_ids = []

    faces = []
    for fid in face_ids:
        try:
            face = app.face_table_client.get_entity(partition_key=user_id, row_key=fid)
            if app._face_is_rejected(face) or not app._face_is_owned_by_person(face, person_id):
                continue
            faces.append({
                'faceId': fid,
                'filename': face.get('filename'),
                'thumbnailUrl': app._face_thumbnail_url(str(face.get('filename') or ''), user_id),
                'bbox': app.json.loads(face.get('bbox', '{}')),
                'imageWidth': int(face.get('imageWidth', 0) or 0),
                'imageHeight': int(face.get('imageHeight', 0) or 0),
                'confidence': float(face.get('confidence', 0.0) or 0.0),
                'reviewStatus': face.get('reviewStatus') or '',
                'suspiciousReason': face.get('suspiciousReason') or '',
            })
        except Exception:
            continue
    faces.sort(key=lambda face: app._face_preview_priority(face), reverse=True)

    return app.jsonify({
        'personId': person_id,
        'name': name,
        'faces': faces,
    })

@people_bp.route('/api/persons/suggestions', methods=['GET'])
def list_person_suggestions():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        try:
            threshold = float(app.request.args.get('threshold', app.PEOPLE_SUGGEST_THRESHOLD))
        except ValueError:
            threshold = app.PEOPLE_SUGGEST_THRESHOLD
        # Never show suggestions below the configured hard minimum.
        threshold = max(threshold, app.MIN_PEOPLE_SUGGEST_THRESHOLD)
        try:
            limit = int(app.request.args.get('limit', app.PEOPLE_SUGGEST_LIMIT))
        except ValueError:
            limit = app.PEOPLE_SUGGEST_LIMIT
        try:
            per_person = int(app.request.args.get('perPerson', app.PEOPLE_SUGGEST_PER_PERSON))
        except ValueError:
            per_person = app.PEOPLE_SUGGEST_PER_PERSON

        suggestions = app._compute_people_suggestions(
            user_id,
            threshold=threshold,
            limit=limit,
            per_person=per_person,
        )
        return app.jsonify({'suggestions': suggestions})
    except Exception as exc:
        app.app.logger.exception('List person suggestions endpoint failed')
        return app.jsonify({'error': 'List person suggestions failed'}), 500

@people_bp.route('/api/persons/suggestions/decline', methods=['POST'])
def decline_person_suggestion():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        data = app.request.get_json(silent=True) or {}
        source_id = str(data.get('sourcePersonId') or '').strip()
        target_id = str(data.get('targetPersonId') or '').strip()
        if not source_id or not target_id:
            return app.jsonify({'error': 'sourcePersonId and targetPersonId required'}), 400
        # Store the decline on both persons so the pair stays hidden regardless
        # of which one ends up being the source next time suggestions run.
        ok_source = app._add_declined_suggestion(user_id, source_id, target_id)
        ok_target = app._add_declined_suggestion(user_id, target_id, source_id)
        if not (ok_source or ok_target):
            return app.jsonify({'error': 'Not found'}), 404
        return app.jsonify({'success': True})
    except Exception as exc:
        app.app.logger.exception('Decline person suggestion endpoint failed')
        return app.jsonify({'error': 'Decline person suggestion failed'}), 500

@people_bp.route('/api/persons/<person_id>/label', methods=['POST'])
def label_person(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    name = data.get('name', '')
    if not isinstance(name, str):
        return app.jsonify({'error': 'Invalid name'}), 400
    ok = app._update_person_entity(user_id, person_id, {'name': name})
    if not ok:
        return app.jsonify({'error': 'Not found'}), 404
    try:
        person = app.person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        face_ids = app.json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []
    affected_files = set()
    for face_id in face_ids:
        try:
            face = app.face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            filename = str(face.get('filename') or '')
            if filename:
                affected_files.add(filename)
            face['confirmedByUser'] = True
            face['reviewStatus'] = 'confirmed'
            face['rejected'] = False
            face.pop('suspiciousReason', None)
            face.pop('rejectedReason', None)
            face.pop('rejectedAt', None)
            face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
            app.face_table_client.upsert_entity(face)
        except Exception:
            continue
    app._update_person_rep_embedding(user_id, person_id)
    app._rebuild_metadata_faces_for_filenames(user_id, affected_files)

    # Naming a cluster is a strong identity signal: use its learned rep to pull
    # this person's faces out of unnamed clusters automatically. Best-effort so a
    # propagation hiccup never fails the label action itself.
    auto_assigned = 0
    if name.strip() and not app._is_unnamed_name(name):
        try:
            propagation = app._propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=False)
            auto_assigned = int(propagation.get('autoAssignedCount') or 0)
        except Exception:
            app.app.logger.exception('Identity propagation after label failed for %s', person_id)
    return app.jsonify({'success': True, 'personId': person_id, 'name': name, 'autoAssignedFaces': auto_assigned})

@people_bp.route('/api/faces/crop/<face_id>', methods=['GET'])
def face_crop(face_id: str):
    """Return a cached cover crop generated from the original image when possible."""
    user_id, error = app._require_user_id()
    if error:
        return error
    if app.face_table_client is None:
        return app.jsonify({'error': 'Face data not available'}), 503
    try:
        entity = app.face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return app.jsonify({'error': 'Not found'}), 404

    filename = entity.get('filename', '')
    bbox_raw = entity.get('bbox', '{}')
    img_w = int(entity.get('imageWidth', 0) or 0)
    img_h = int(entity.get('imageHeight', 0) or 0)

    if not filename or img_w <= 0 or img_h <= 0:
        return app.jsonify({'error': 'Incomplete face data'}), 422

    try:
        bbox = app.json.loads(bbox_raw) if isinstance(bbox_raw, str) else bbox_raw
    except Exception:
        return app.jsonify({'error': 'Invalid face bbox'}), 422

    x = int(bbox.get('left', bbox.get('x', 0)) or 0)
    y = int(bbox.get('top', bbox.get('y', 0)) or 0)
    w = int(bbox.get('width', 0))
    h = int(bbox.get('height', 0))
    if w <= 0 or h <= 0:
        return app.jsonify({'error': 'Invalid bbox dimensions'}), 422

    cover_blob = f"{app.hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:16]}/{app.secure_filename(face_id)}.jpg"
    try:
        props = app.get_media_properties('cover', cover_blob)
        if props:
            # face_id is content-addressed (hash of filename+bbox, see
            # _deterministic_face_id) so a persisted cover is immutable for
            # its lifetime — safe to cache long. SAS URLs are day-aligned and
            # valid up to 48h (see _create_stable_read_sas_url), so 1h keeps
            # this well inside that window.
            resp = app.jsonify({'url': app.make_media_url(cover_blob, 'cover')})
            resp.headers['Cache-Control'] = 'public, max-age=3600, immutable'
            return resp
    except Exception:
        pass

    # The face's source photo may be stored under an anonymous UUID; resolve the
    # physical blob for both the image read and the thumbnail fallback below.
    source_blob = app._resolve_media_blob_name(user_id, filename)

    # Shared fallback: crop from the pre-generated thumbnail instead of the
    # original. Used both when the original can't be downloaded and when it
    # downloads fine but can't be decoded (e.g. a RAW/HEIC file where
    # conversion below also fails) -- either way we still have a usable image.
    def _crop_from_thumbnail():
        thumb_bytes = app.download_media_bytes('thumbnail', source_blob)
        with app.Image.open(app.io.BytesIO(thumb_bytes)) as img:
            tw, th = img.size
            sx = tw / img_w
            sy = th / img_h
            pad = max(1, int(min(w, h) * 0.15))
            left = max(0, int(x * sx) - pad)
            top = max(0, int(y * sy) - pad)
            right = min(tw, int((x + w) * sx) + pad)
            bottom = min(th, int((y + h) * sy) + pad)
            cropped = img.crop((left, top, right, bottom))
            buf = app.io.BytesIO()
            cropped.convert('RGB').save(buf, format='JPEG', quality=85)
            buf.seek(0)
            return 'data:image/jpeg;base64,' + app.base64.b64encode(buf.read()).decode('ascii')

    try:
        image_bytes = app.download_media_bytes('image', source_blob)
    except Exception:
        try:
            return app.jsonify({'url': _crop_from_thumbnail()})
        except Exception:
            return app.jsonify({'error': 'Image not available'}), 404

    # RAW/cinema-RAW/HEIC originals aren't directly decodable the way a plain
    # JPEG is (same check the preview pipeline uses, _filename_requires_backend_preview)
    # -- extract a real preview first so Image.open below doesn't blow up with
    # PIL.UnidentifiedImageError on bytes it can't parse.
    if app._filename_requires_backend_preview(filename):
        try:
            converted = app.convert_image_to_jpeg(image_bytes, filename)
            if converted:
                image_bytes = converted
        except Exception:
            pass

    try:
        with app.Image.open(app.io.BytesIO(image_bytes)) as img:
            img = app.ImageOps.exif_transpose(img)
            try:
                metadata = app._get_metadata_entity(user_id, filename) or {}
                rotation = app._normalize_rotation(metadata.get('rotation', 0))
            except Exception:
                rotation = 0
            if rotation:
                img = img.rotate(-rotation, expand=True)
            tw, th = img.size
            sx = tw / img_w
            sy = th / img_h
            pad = max(1, int(min(w, h) * 0.35))
            left = max(0, int(x * sx) - pad)
            top = max(0, int(y * sy) - pad)
            right = min(tw, int((x + w) * sx) + pad)
            bottom = min(th, int((y + h) * sy) + pad)
            cropped = img.crop((left, top, right, bottom))
            cropped.thumbnail((512, 512), app.Image.Resampling.LANCZOS if hasattr(app.Image, 'Resampling') else app.Image.LANCZOS)
            buf = app.io.BytesIO()
            cropped.convert('RGB').save(buf, format='JPEG', quality=88, optimize=True)
            buf.seek(0)
            cover_bytes = buf.read()
            try:
                app.upload_media_file('cover', cover_blob, cover_bytes, 'image/jpeg')
                resp = app.jsonify({'url': app.make_media_url(cover_blob, 'cover')})
                resp.headers['Cache-Control'] = 'public, max-age=3600, immutable'
                return resp
            except Exception:
                data_url = 'data:image/jpeg;base64,' + app.base64.b64encode(cover_bytes).decode('ascii')
    except Exception:
        try:
            return app.jsonify({'url': _crop_from_thumbnail()})
        except Exception:
            return app.jsonify({'error': 'Image not available'}), 404

    return app.jsonify({'url': data_url})

@people_bp.route('/api/persons/<person_id>/confirm-face', methods=['POST'])
def confirm_face(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_id = data.get('faceId')
    if not face_id:
        return app.jsonify({'error': 'faceId required'}), 400

    try:
        app.person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return app.jsonify({'error': 'person not found'}), 404
    try:
        face = app.face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return app.jsonify({'error': 'face not found'}), 404

    old_person_id = face.get('personId')
    if old_person_id and old_person_id != person_id:
        app._remove_face_from_person(user_id, str(old_person_id), face_id)
    app._remove_face_from_other_people(user_id, face_id, person_id)
    app._add_face_to_person(user_id, person_id, face_id)
    face['personId'] = person_id
    face['confirmedByUser'] = True
    face['reviewStatus'] = 'confirmed'
    face['rejected'] = False
    face.pop('suspiciousReason', None)
    face.pop('rejectedReason', None)
    face.pop('rejectedAt', None)
    face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
    app.face_table_client.upsert_entity(face)
    filename = face.get('filename')
    if filename:
        app._rebuild_metadata_faces_for_filename(user_id, filename)
    app._update_person_rep_embedding(user_id, person_id)
    return app.jsonify({'success': True, 'personId': person_id, 'faceId': face_id})

@people_bp.route('/api/persons/<person_id>/find-faces', methods=['POST'])
def find_person_faces(person_id: str):
    """Start (or run) identity propagation for this named person.

    Queue-first: long-running full-table scans run on the background worker when
    available. If the queue is unavailable (local/dev/no worker), fall back to
    inline execution so the feature still functions.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    try:
        app.person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return app.jsonify({'error': 'person not found'}), 404

    queued = app._enqueue_propagate_job(user_id, person_id)
    if queued.get('status') == 'queued':
        return app.jsonify({
            'success': True,
            'queued': True,
            'status': 'queued',
            'personId': person_id,
            'propagateJobId': queued.get('jobId'),
            'autoAssignedFaces': 0,
            'autoAssigned': [],
            'suggestions': [],
            'candidateFaces': 0,
        })

    try:
        result = app._propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=True)
    except Exception as exc:
        app.app.logger.exception('find_person_faces failed for %s', person_id)
        return app.jsonify({'error': 'Find faces failed'}), 500
    return app.jsonify({
        'success': True,
        'queued': False,
        'status': 'done',
        'personId': person_id,
        'propagateJobId': None,
        'autoAssignedFaces': int(result.get('autoAssignedCount') or 0),
        'autoAssigned': result.get('autoAssigned') or [],
        'suggestions': result.get('suggestions') or [],
        'candidateFaces': int(result.get('candidateFaces') or 0),
        'skipped': result.get('skipped'),
    })

@people_bp.route('/api/persons/<person_id>/suggested-faces/accept', methods=['POST'])
def accept_suggested_faces(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return app.jsonify({'error': 'faceIds must be a list'}), 400
    try:
        app.person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return app.jsonify({'error': 'person not found'}), 404

    accepted = []
    affected_files = set()
    for raw_face_id in face_ids:
        face_id = str(raw_face_id or '').strip()
        if not face_id:
            continue
        try:
            face = app.face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            continue
        old_person_id = str(face.get('personId') or '')
        if old_person_id and old_person_id != person_id:
            app._remove_face_from_person(user_id, old_person_id, face_id)
        app._remove_face_from_other_people(user_id, face_id, person_id)
        app._add_face_to_person(user_id, person_id, face_id)
        face['personId'] = person_id
        face['confirmedByUser'] = True
        face['reviewStatus'] = 'confirmed'
        face['rejected'] = False
        face.pop('assignedByPropagation', None)
        face.pop('suspiciousReason', None)
        face.pop('rejectedReason', None)
        face.pop('rejectedAt', None)
        face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
        try:
            app.face_table_client.upsert_entity(face)
        except Exception:
            continue
        filename = str(face.get('filename') or '')
        if filename:
            affected_files.add(filename)
        accepted.append(face_id)

    if accepted:
        app._update_person_rep_embedding(user_id, person_id)
        app._rebuild_metadata_faces_for_filenames(user_id, affected_files)
    return app.jsonify({'success': True, 'personId': person_id, 'acceptedFaces': len(accepted), 'accepted': accepted})

@people_bp.route('/api/persons/<person_id>/suggested-faces/decline', methods=['POST'])
def decline_suggested_faces(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return app.jsonify({'error': 'faceIds must be a list'}), 400
    declined = app._add_declined_face_suggestions(user_id, person_id, [str(fid) for fid in face_ids])
    return app.jsonify({'success': True, 'personId': person_id, 'declinedFaces': declined})

@people_bp.route('/api/persons/<person_id>/delete', methods=['POST', 'DELETE'])
def delete_person_cluster(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    result = app._delete_person_cluster(user_id, person_id)
    if not result.get('deleted'):
        return app.jsonify({'error': 'person not found'}), 404
    return app.jsonify({'success': True, 'personId': person_id, **result})

@people_bp.route('/api/persons/delete', methods=['POST'])
def delete_person_clusters():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    person_ids = data.get('personIds', [])
    if not isinstance(person_ids, list):
        return app.jsonify({'error': 'personIds must be a list'}), 400

    deleted_person_ids = []
    errors = []
    affected_filenames = set()
    faces_updated = 0
    for raw_person_id in person_ids:
        person_id_value = str(raw_person_id or '').strip()
        if not person_id_value:
            continue
        result = app._delete_person_cluster(user_id, person_id_value, rebuild_metadata=False)
        if result.get('deleted'):
            deleted_person_ids.append(person_id_value)
            faces_updated += int(result.get('facesUpdated') or 0)
            affected_filenames.update(result.get('filenames') or [])
        else:
            errors.append({'personId': person_id_value, 'error': 'person not found'})

    metadata_rebuild = app._rebuild_metadata_faces_for_filenames(user_id, affected_filenames)
    return app.jsonify({
        'success': len(errors) == 0,
        'deletedPersonIds': deleted_person_ids,
        'errors': errors,
        'facesUpdated': faces_updated,
        'metadataRebuild': metadata_rebuild,
    })

@people_bp.route('/api/persons/<person_id>/merge', methods=['POST'])
def merge_persons(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    merge_ids = data.get('mergeIds', [])
    if not isinstance(merge_ids, list):
        return app.jsonify({'error': 'mergeIds must be a list'}), 400

    core = app._merge_persons_core(user_id, person_id, merge_ids)
    if core is None:
        return app.jsonify({'error': 'base person not found'}), 404
    merge_id = core['mergeId']

    # If the merged-into person is named, reuse its strengthened rep to reclaim
    # matching faces still sitting in unnamed clusters. That scans the entire face
    # table (the slowest part of a merge, and a past OOM driver), so hand it to the
    # queue-scaled worker instead of blocking this request; the reclaimed faces
    # surface on the next refresh. If the queue is unavailable, fall back to running
    # it inline so behaviour is unchanged when there is no worker.
    auto_assigned = 0
    propagate_job_id = None
    if app._person_is_named(user_id, person_id):
        queued = app._enqueue_propagate_job(user_id, person_id)
        if queued.get('status') == 'queued':
            propagate_job_id = queued.get('jobId')
        else:
            try:
                propagation = app._propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=False)
                auto_assigned = int(propagation.get('autoAssignedCount') or 0)
            except Exception:
                app.app.logger.exception('Identity propagation after merge failed for %s', person_id)

    return app.jsonify({
        'success': True,
        'personId': person_id,
        'mergeId': merge_id,
        'autoAssignedFaces': auto_assigned,
        'propagateJobId': propagate_job_id,
    })

@people_bp.route('/api/persons/merge/batch', methods=['POST'])
def merge_persons_batch():
    """Bulk-approve several merge-suggestion pairs in one request.

    Each pair's face reassignment still runs sequentially (they're independent
    person ids, so this is safe), but identity propagation — the expensive
    full face-table scan that reclaims a named person's faces from unnamed
    clusters — is coalesced into a single background job covering every named
    target in the batch, instead of one job per pair. Approving suggestions
    one request at a time previously queued one propagate job per approval;
    the worker drains the clustering queue one message at a time, so a big
    batch drained as a slow drip of individual completion toasts over several
    minutes. See [[job-completion-notifications]] / bombardment fix.
    """
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    pairs = data.get('merges', [])
    if not isinstance(pairs, list) or not pairs:
        return app.jsonify({'error': 'merges must be a non-empty list'}), 400
    if len(pairs) > app.PEOPLE_MERGE_BATCH_MAX:
        return app.jsonify({'error': f'too many merges in one batch (max {app.PEOPLE_MERGE_BATCH_MAX})'}), 400

    results = []
    named_target_ids: app.List[str] = []
    seen_targets = set()
    for pair in pairs:
        target_id = str((pair or {}).get('targetPersonId') or (pair or {}).get('personId') or '') if isinstance(pair, dict) else ''
        source_ids = pair.get('mergeIds') if isinstance(pair, dict) else None
        if not target_id or not isinstance(source_ids, list) or not source_ids:
            results.append({'targetPersonId': target_id, 'success': False, 'error': 'invalid pair'})
            continue
        core = app._merge_persons_core(user_id, target_id, source_ids)
        if core is None:
            results.append({'targetPersonId': target_id, 'success': False, 'error': 'base person not found'})
            continue
        results.append({'targetPersonId': target_id, 'success': True, 'mergeId': core['mergeId']})
        if target_id not in seen_targets and app._person_is_named(user_id, target_id):
            seen_targets.add(target_id)
            named_target_ids.append(target_id)

    propagate_job_id = None
    auto_assigned_total = 0
    if named_target_ids:
        queued = app._enqueue_propagate_batch_job(user_id, named_target_ids)
        if queued.get('status') == 'queued':
            propagate_job_id = queued.get('jobId')
        else:
            # No worker available (local/dev): fall back to running each pass
            # inline so behaviour is unchanged when there is no queue.
            for target_id in named_target_ids:
                try:
                    propagation = app._propagate_person_identity(user_id, target_id, apply=True, collect_suggestions=False)
                    auto_assigned_total += int(propagation.get('autoAssignedCount') or 0)
                except Exception:
                    app.app.logger.exception('Identity propagation after batch merge failed for %s', target_id)

    return app.jsonify({
        'success': all(r.get('success') for r in results),
        'results': results,
        'propagateJobId': propagate_job_id,
        'autoAssignedFaces': auto_assigned_total,
        'targetPersonIds': named_target_ids,
    })

@people_bp.route('/api/persons/merge/<merge_id>/undo', methods=['POST'])
def undo_merge(merge_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    try:
        merge_entry = app.merge_table_client.get_entity(partition_key=user_id, row_key=merge_id)
    except Exception:
        return app.jsonify({'error': 'merge not found'}), 404

    try:
        payload = app.json.loads(merge_entry.get('payload', '{}'))
    except Exception:
        return app.jsonify({'error': 'invalid merge payload'}), 500

    base = payload.get('base') or {}
    merged = payload.get('merged') or []
    face_map = payload.get('faceMap') or {}
    affected_face_ids = set(str(fid) for fid in face_map.keys())
    try:
        affected_face_ids.update(str(fid) for fid in app.json.loads(base.get('faceIds', '[]') or '[]'))
    except Exception:
        pass
    for item in merged:
        try:
            affected_face_ids.update(str(fid) for fid in app.json.loads(item.get('faceIds', '[]') or '[]'))
        except Exception:
            pass

    if base and 'PartitionKey' in base and 'RowKey' in base:
        try:
            app.person_table_client.upsert_entity(base)
        except Exception:
            pass

    for m in merged:
        if 'PartitionKey' in m and 'RowKey' in m:
            try:
                app.person_table_client.upsert_entity(m)
            except Exception:
                pass

    for fid, original_pid in face_map.items():
        try:
            face_ent = app.face_table_client.get_entity(partition_key=user_id, row_key=fid)
            if original_pid:
                face_ent['personId'] = original_pid
            else:
                face_ent.pop('personId', None)
            face_ent.pop('confirmedByUser', None)
            try:
                current_confidence = float(face_ent.get('confidence', 0.0) or 0.0)
            except Exception:
                current_confidence = 0.0
            face_ent['confidence'] = min(current_confidence if current_confidence > 0 else 0.8, 0.95)
            app.face_table_client.upsert_entity(face_ent)
        except Exception:
            pass

    affected_person_ids = set()
    if base.get('RowKey'):
        affected_person_ids.add(str(base['RowKey']))
    for m in merged:
        if m.get('RowKey'):
            affected_person_ids.add(str(m['RowKey']))
    for person_id in affected_person_ids:
        app._update_person_rep_embedding(user_id, person_id)
    app._rebuild_metadata_faces_for_filenames(user_id, app._filenames_for_face_ids(user_id, list(affected_face_ids)))

    try:
        app.merge_table_client.delete_entity(partition_key=user_id, row_key=merge_id)
    except Exception:
        pass

    return app.jsonify({'success': True, 'mergeId': merge_id})

@people_bp.route('/api/persons/merges', methods=['GET'])
def list_merges():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    # This partition also holds the people-repair snapshots (recluster / dedupe /
    # suspicious / membership), which serialize the entire people+faces+metadata
    # tables into multi-KB `payload` chunk rows (see _create_people_repair_snapshot).
    # Pulling full entities here materialised every one of those payloads into
    # memory just to skip them by `kind` on the next line — enough to OOM-kill the
    # replica on a large library. Project only the small columns the undo list
    # needs (never `payload`) and stream the rows instead of list()-ing them, so
    # the snapshot backups are never transferred or held in memory.
    select_cols = ['RowKey', 'kind', 'targetPersonId', 'mergedIds', 'targetName', 'mergedNames', 'createdAt']
    try:
        rows_iter = app.merge_table_client.query_entities(
            f"PartitionKey eq '{app._escape_odata(user_id)}'",
            select=select_cols,
        )
    except Exception:
        return app.jsonify({'merges': []})

    merges = []
    try:
        for row in rows_iter:
            try:
                if str(row.get('kind') or '').startswith(('recluster_snapshot', 'face_dedupe_snapshot', 'suspicious_face_snapshot', 'unblock_faces_snapshot', 'face_membership_snapshot')):
                    continue
                try:
                    merged_names = app.json.loads(row.get('mergedNames', '[]') or '[]')
                except Exception:
                    merged_names = []
                try:
                    merged_ids = app.json.loads(row.get('mergedIds', '[]') or '[]')
                except Exception:
                    merged_ids = []
                merges.append({
                    'mergeId': row['RowKey'],
                    'targetPersonId': row.get('targetPersonId'),
                    'mergedIds': merged_ids,
                    'targetName': row.get('targetName'),
                    'mergedNames': merged_names,
                    'createdAt': row.get('createdAt'),
                })
            except Exception:
                continue
    except Exception:
        # A mid-stream paging error still returns whatever was collected.
        pass
    return app.jsonify({'merges': merges})

@people_bp.route('/api/persons/<person_id>/not-face', methods=['POST'])
@people_bp.route('/persons/<person_id>/not-face', methods=['POST'])
def mark_not_face(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_id = str(data.get('faceId') or '').strip()
    if not face_id:
        return app.jsonify({'error': 'faceId required'}), 400
    result = app._mark_face_not_a_face(user_id, person_id, face_id)
    status = int(result.pop('status', 200))
    return app.jsonify(result), status

@people_bp.route('/api/persons/<person_id>/separate', methods=['POST'])
def separate_face(person_id: str):
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_id = data.get('faceId')
    if not face_id:
        return app.jsonify({'error': 'faceId required'}), 400

    result = app._split_face_into_new_person(user_id, person_id, str(face_id))
    status = int(result.pop('status', 200))
    return app.jsonify(result), status

@people_bp.route('/api/faces', methods=['GET'])
def list_faces():
    """Flat list of the user's active faces across every person.

    Powers the Faces grid, which shows individual face crops (rather than the
    per-person groupings in the Clusters view) so many faces fit in one page.
    """
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        q = (app.request.args.get('q') or '').strip().lower()
        try:
            offset = int(app.request.args.get('offset', '0'))
            limit = int(app.request.args.get('limit', '50'))
        except ValueError:
            return app.jsonify({'error': 'Invalid paging parameters.'}), 400

        person_rows, face_by_id = app._scan_person_and_face_rows(user_id)

        # Phase A: cheap pass building the ordered (person, face) pairs using
        # only the bulk face map -- no per-face network calls, no SAS minting.
        # The expensive per-face summary (Phase B, below) only runs for the
        # requested page slice.
        entries = []
        seen = set()
        unnamed_counter = 1
        for person in person_rows:
            try:
                person_id = str(person.get('RowKey') or '')
                if not person_id:
                    continue
                name = str(person.get('name', '') or '').strip()
                if not name:
                    name = f'Unnamed {unnamed_counter}'
                    unnamed_counter += 1
                if q and q not in name.lower():
                    continue
                try:
                    face_ids = app.json.loads(person.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                for face_id in face_ids:
                    fid = str(face_id or '')
                    if not fid or fid in seen:
                        continue
                    # A bulk-map miss moments after the scan above is almost
                    # always a genuinely deleted face -- unlike list_persons,
                    # there's no cluster-level "keep it anyway" decision at
                    # stake here, so this cheap pass simply excludes it rather
                    # than paying for an individual lookup.
                    face = face_by_id.get(fid)
                    if face is None:
                        continue
                    if app._face_is_rejected(face) or not app._face_is_owned_by_person(face, person_id):
                        continue
                    seen.add(fid)
                    entries.append({'personId': person_id, 'personName': name, 'faceId': fid})
            except Exception:
                continue

        total = len(entries)
        faces = []
        for entry in entries[offset:offset + limit]:
            try:
                face = face_by_id.get(entry['faceId'])
                if face is None:
                    continue
                summary = app._face_summary_for_person_list(entry['faceId'], face, user_id)
                summary['personId'] = entry['personId']
                summary['personName'] = entry['personName']
                faces.append(summary)
            except Exception:
                continue
        return app.jsonify({'faces': faces, 'total': total})
    except Exception as exc:
        app.app.logger.exception('List faces endpoint failed')
        return app.jsonify({'error': 'List faces failed'}), 500

@people_bp.route('/api/faces/delete', methods=['POST'])
def delete_faces():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app._people_features_available():
        return app.jsonify({'error': 'People features not configured'}), 503
    data = app.request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return app.jsonify({'error': 'faceIds must be a list'}), 400
    result = app._delete_faces_bulk(user_id, [str(fid or '') for fid in face_ids])
    status = int(result.pop('status', 200))
    return app.jsonify({'success': len(result.get('errors') or []) == 0, **result}), status

@people_bp.route('/api/people/diagnostic', methods=['GET'])
def people_diagnostic():
    """Diagnostic endpoint: why aren't faces clustering into people?"""
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503

        # Fetch all faces and people
        try:
            faces = list(app.face_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'")) if app.face_table_client else []
        except Exception:
            faces = []
        try:
            people = list(app.person_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'")) if app.person_table_client else []
        except Exception:
            people = []

        # Analyze face clustering eligibility
        total_faces = len(faces)
        accepted_faces = 0
        rejected_faces = 0
        suspicious_faces = 0
        low_confidence_faces = 0
        stale_embedding_version_faces = 0
        no_embedding_faces = 0
        unassigned_faces = 0
        confirmed_faces = 0

        allowed_versions = app._face_embedding_allowed_versions()

        for face in faces:
            if app._face_is_rejected(face):
                rejected_faces += 1
                continue
            if app._face_is_suspicious(face):
                suspicious_faces += 1
                continue
            if not app._face_embedding_allowed_for_clustering(face):
                stale_embedding_version_faces += 1
                continue
            emb = app._face_embedding_from_entity(face)
            if not emb:
                no_embedding_faces += 1
                continue
            if app._coerce_bool(face.get('confirmedByUser', False)):
                confirmed_faces += 1
            if not str(face.get('personId') or '').strip():
                unassigned_faces += 1
            accepted_faces += 1

        # Check for active clustering job
        active_job = app._has_active_clustering_job(user_id)

        # Check configuration
        clustering_available = app.clustering_queue_client is not None
        browser_only = app.BROWSER_ONLY_PROCESSING

        return app.jsonify({
            'totalFaces': total_faces,
            'acceptedForClustering': accepted_faces,
            'rejectedFaces': rejected_faces,
            'suspiciousFaces': suspicious_faces,
            'lowConfidenceFaces': low_confidence_faces,
            'staleEmbeddingVersionFaces': stale_embedding_version_faces,
            'noEmbeddingFaces': no_embedding_faces,
            'unassignedFaces': unassigned_faces,
            'confirmedFaces': confirmed_faces,
            'totalPeople': len(people),
            'clusteringConfiguration': {
                'browserOnlyProcessing': browser_only,
                'clusteringQueueAvailable': clustering_available,
                'activeClusteringJob': active_job,
                'allowedEmbeddingVersions': list(allowed_versions),
                'clusteringEps': app.PEOPLE_CLUSTER_EPS,
                'clusteringPreset': app.PEOPLE_CLUSTER_PRESET,
            },
            'recommendation': (
                'No faces detected.' if total_faces == 0
                else 'All faces rejected or below confidence threshold.' if accepted_faces == 0
                else 'Clustering queue not available; check BROWSER_ONLY_PROCESSING.' if not clustering_available
                else f'Trigger clustering with POST /api/people/assign-unclustered or POST /api/people/recluster (with repair confirmation).' if unassigned_faces > 0 or len(people) == 0
                else f'All {accepted_faces} faces already assigned to people.'
            ),
        })
    except Exception as exc:
        app.app.logger.exception('People diagnostic failed')
        return app.jsonify({'error': 'Diagnostic failed'}), 500

@people_bp.route('/people/recluster', methods=['POST'])
@people_bp.route('/api/people/recluster', methods=['POST'])
def recluster_people():
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
        app.app.logger.exception('People recluster route failed')
        return app.jsonify({'error': 'People recluster failed'}), 500

@people_bp.route('/api/people/assign-unclustered', methods=['POST'])
@people_bp.route('/people/assign-unclustered', methods=['POST'])
def assign_unclustered_people():
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if not app._people_features_available():
            return app.jsonify({'error': 'People features not configured'}), 503
        return app.jsonify(app._assign_unclustered_faces(user_id))
    except Exception as exc:
        app.app.logger.exception('Assign unclustered faces route failed')
        return app.jsonify({'error': 'Assign unclustered faces failed'}), 500
