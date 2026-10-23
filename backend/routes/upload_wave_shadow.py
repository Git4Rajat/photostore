"""Explicit opt-in shadow intake API; intentionally no automatic dispatch."""
from functools import wraps
import json
import os

from azure.core.exceptions import AzureError, ResourceNotFoundError

import app
from upload_waves import Conflict, ShadowWaves
import upload_wave_shadow as source


def _public(row):
    result = {key: value for key, value in row.items()
              if key not in ('PartitionKey', 'RowKey', 'Timestamp', 'evaluation')}
    if 'evaluation' in row:
        progress = json.loads(row.get('evaluation') or '{}')
        result.update(scanInProgress=bool(progress) and row.get('state') != 'ready',
                      scannedMembers=progress.get('members', 0),
                      scannedSessions=progress.get('sessions', 0))
    return result


def endpoint(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if os.getenv('UPLOAD_WAVE_SHADOW_ENABLED', '').strip().lower() != 'true':
            return app.jsonify({'error': 'Shadow upload protocol is disabled'}), 404
        _, library, error = app._require_library_context(require_auth=True)
        if error:
            return error
        if app.request.content_length and app.request.content_length > 65536:
            return app.jsonify({'error': 'Shadow request too large'}), 413
        if app.jobs_table_client is None:
            return app.jsonify({'error': 'Shadow protocol storage unavailable'}), 503
        data = app.request.get_json(silent=True) if app.request.method == 'POST' else {}
        if not isinstance(data, dict):
            return app.jsonify({'error': 'Expected a JSON object'}), 400
        try:
            result = function(ShadowWaves(app.jobs_table_client, library), data, *args, **kwargs)
            response = app.jsonify(dict(_public(result), protocolMode='shadow', dispatchable=False, evidenceQualified=False))
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ResourceNotFoundError:
            return app.jsonify({'error': 'Finalized photo not found'}), 409
        except Conflict as error:
            return app.jsonify({'error': str(error)}), 409
        except (ValueError, KeyError, TypeError):
            return app.jsonify({'error': 'Invalid shadow protocol request'}), 400
        except AzureError:
            app.app.logger.exception('Shadow upload protocol storage failure')
            return app.jsonify({'error': 'Shadow protocol storage unavailable'}), 503
    return wrapped


def register_routes(blueprint):
    @blueprint.route('/upload/waves/shadow/sessions', methods=['POST'])
    @endpoint
    def wave_shadow_open(store, data):
        return store.open_session(data['sessionId'], data['declaredCount'])

    @blueprint.route('/upload/waves/shadow/sessions/<session>/items', methods=['POST'])
    @endpoint
    def wave_shadow_register(store, data, session):
        return store.register(session, data['items'])

    @blueprint.route('/upload/waves/shadow/sessions/<session>/resolve', methods=['POST'])
    @endpoint
    def wave_shadow_resolve(store, data, session):
        if app.metadata_table_client is None and data['outcome'] in ('accepted', 'duplicate'):
            raise AzureError('Metadata storage unavailable')
        filename = app._validate_media_filename(data.get('filename', ''))
        if data['outcome'] in ('accepted', 'duplicate') and not filename:
            raise ValueError('Finalized filename required')
        return source.resolve_source(store, app.metadata_table_client, session,
                                     data['itemId'], data['outcome'], filename)

    @blueprint.route('/upload/waves/shadow/sessions/<session>/close', methods=['POST'])
    @endpoint
    def wave_shadow_close(store, data, session):
        # Explicit resolution is required even for stopped/failed selections.
        return store.close(session, data['manifestRevision'], data['closeId'])

    @blueprint.route('/upload/waves/shadow/<wave>/reconcile', methods=['POST'])
    @endpoint
    def wave_shadow_reconcile(store, data, wave):
        if app.metadata_table_client is None:
            raise AzureError('Metadata storage unavailable')
        return source.reconcile_page(store, wave, app.metadata_table_client,
                                     app.face_table_client, app.get_face_ids_for_filename,
                                     after=data.get('after', ''))

    @blueprint.route('/upload/waves/shadow/<wave>/evaluate', methods=['POST'])
    @endpoint
    def wave_shadow_evaluate(store, data, wave):
        return store.evaluate(wave)

    @blueprint.route('/upload/waves/shadow/<wave>', methods=['GET'])
    @endpoint
    def wave_shadow_status(store, data, wave):
        return store.status(wave)
