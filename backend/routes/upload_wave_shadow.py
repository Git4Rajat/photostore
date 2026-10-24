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
        live='/waves/live/' in app.request.path
        if not live and os.getenv('UPLOAD_WAVE_SHADOW_ENABLED', '').strip().lower() != 'true':
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
            store=ShadowWaves(app.jobs_table_client,library)
            from wave_policy import owned,configuration_ready,require_capability
            if not live and owned(store.table,library):raise Conflict('Use the live wave protocol for this library')
            if live:
                if not owned(store.table,library) or not configuration_ready():
                    raise Conflict('Library is not enabled for the live wave protocol')
                require_capability(store,'upload')
                if store._required('policy').get('cleanupState'):raise Conflict('Library cleanup is in progress')
            result = function(store, data, *args, **kwargs)
            response = app.jsonify(dict(_public(result), protocolMode='live' if live else 'shadow', dispatchable=False, evidenceQualified=False))
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
    @blueprint.route('/upload/waves/live/sessions', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions', methods=['POST'])
    @endpoint
    def wave_shadow_open(store, data):
        return store.open_session(data['sessionId'], data['declaredCount'],
                                  resumed_from=data.get('resumedFromSessionId', ''))

    @blueprint.route('/upload/waves/live/sessions/<session>/items', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/items', methods=['POST'])
    @endpoint
    def wave_shadow_register(store, data, session):
        return store.register(session, data['items'])

    @blueprint.route('/upload/waves/live/sessions/<session>/resolve', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/resolve', methods=['POST'])
    @endpoint
    def wave_shadow_resolve(store, data, session):
        if app.metadata_table_client is None and data['outcome'] in ('accepted', 'duplicate'):
            raise AzureError('Metadata storage unavailable')
        filename = app._validate_media_filename(data.get('filename', ''))
        if data['outcome'] in ('accepted', 'duplicate') and not filename:
            raise ValueError('Finalized filename required')
        result=source.resolve_source(store, app.metadata_table_client, session,
                                     data['itemId'], data['outcome'], filename)
        return close_resolved_intake(store,result) if '/waves/live/' in app.request.path else result

    @blueprint.route('/upload/waves/live/sessions/<session>/close', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/close', methods=['POST'])
    @endpoint
    def wave_shadow_close(store, data, session):
        # Explicit resolution is required even for stopped/failed selections.
        result=store.close(session, data['manifestRevision'], data['closeId'])
        if '/waves/live/' in app.request.path:
            _drive(store,result['waveId'])
        return result

    @blueprint.route('/upload/waves/live/sessions/<session>/abandon', methods=['POST'])
    @endpoint
    def wave_live_abandon(store,data,session):
        account,_,error=app._require_library_context(require_auth=True)
        if error:return error
        if not app.library_store.is_owner(account,store.library):
            raise Conflict('Only the library owner may abandon intake')
        from wave_settlement import abandon
        result=abandon(store,session,data['manifestRevision'],reason=data['reason'])
        _drive(store,result['waveId'])
        return result

    @blueprint.route('/upload/waves/live/<wave>/repair',methods=['POST'])
    @endpoint
    def wave_live_repair(store,data,wave):
        account,_,error=app._require_library_context(require_auth=True)
        if error or not app.library_store.is_owner(account,store.library):
            raise Conflict('Only the library owner may repair a wave')
        current=store._required(store.wave_key(wave));action=data['action']
        from upload_waves import digest
        if action=='retry-stage':
            from upload_wave_execution import stage_work_store
            from execution_lease import ExecutionLeases
            stage=('vision' if current['state'] in ('processing_pending','processing') else
                'cluster' if current['state']=='ready' else 'index' if current['state']=='indexing' else None)
            if stage is None:raise Conflict('Wave has no retryable stage')
            lease=store._get(ExecutionLeases.key(store.library,wave,stage))
            if lease and lease.get('leaseUntil',0)>store.clock():raise Conflict('Stage owner is still active')
            key=(current.get('visionPendingKey') if stage=='vision' else
                'shadow-cluster!'+wave if stage=='cluster' else current.get('indexPendingKey'))
            stage_work_store(store.table).request(store.library,f'wave-{stage}:{wave}:{digest(key)}',advance=True)
        else:
            member=store._required(f'e!{wave}!m!'+digest([data['photoId'],data['processingRevision']]))
            if action=='confirm-missing':
                try:app.metadata_table_client.get_entity(store.library,member['filename'])
                except ResourceNotFoundError:pass
                else:raise Conflict('Source still exists; missing-source settlement refused')
                if not data.get('reason'):raise ValueError('Operator reason required')
                from wave_settlement import settle
                settle(store,wave,member,'excluded','operator_confirmed_missing',witness=dict(reason=data['reason']))
            elif action=='retry-source':
                from processing_generation import REQUIRED_STEPS
                result=app._enqueue_processing_steps(store.library,member['filename'],list(REQUIRED_STEPS),force=True)
                if any(value.get('status')=='error' for value in result.values()):raise Conflict('Source reprocessing admission failed')
            else:raise ValueError('Unknown repair action')
        _drive(store,wave)
        return store.status(wave)

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

    @blueprint.route('/upload/waves/live/<wave>', methods=['GET'])
    @blueprint.route('/upload/waves/shadow/<wave>', methods=['GET'])
    @endpoint
    def wave_shadow_status(store, data, wave):
        return store.status(wave)


def _drive(store,wave_id):
    from upload_wave_execution import advance_wave,stage_work_store,release_and_relay,execution_enabled
    if not execution_enabled():return
    from wave_runtime import send_stage
    work=stage_work_store(store.table)
    wave,_=advance_wave(work,store,store.library,wave_id)
    state=wave['state']
    destination=('vision' if state in ('processing_pending','processing') else
        'cluster' if state=='ready' and not wave.get('zeroFaceProbePending') else 'index' if state=='indexing' else None)
    key=(wave.get('visionPendingKey') if destination=='vision' else
         'shadow-cluster!'+wave_id if destination=='cluster' else wave.get('indexPendingKey'))
    if destination and key:release_and_relay(work,store,store.library,key,destination,send_stage)


def close_resolved_intake(store,session):
    if (session['state']=='open' and session['registeredCount']==session['declaredCount']
            and session['resolvedCount']==session['declaredCount']):
        session=store.close(session['sessionId'],session['manifestRevision'],'auto-complete')
        _drive(store,session['waveId'])
    return session
