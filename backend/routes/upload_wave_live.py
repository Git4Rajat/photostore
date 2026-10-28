"""Authoritative admission fences for libraries born into the wave protocol."""
from flask import g
import os
from azure.core.exceptions import AzureError
from upload_waves import ShadowWaves,Conflict,IntakeBindingConflict
import wave_policy


def finalized_replay(data):
    """The guard verifies resolved items before any finalization can write."""
    return getattr(g,'wave_finalize_replays',{}).get((data.get('waveSessionId'),data.get('waveItemId')))


def _verified_replay(store, binding, data, metadata_table):
    from upload_wave_shadow import source_identity
    from azure.core.exceptions import ResourceNotFoundError
    item=store._required(store.item_key(*binding))
    if item['outcome'] not in ('accepted','duplicate'):return None
    try:
        source=metadata_table.get_entity(store.library,item['acceptedFilename'])
    except ResourceNotFoundError as error:
        raise IntakeBindingConflict(['sourceMissing']) from error
    try:
        photo,revision,_=source_identity(source)
    except Conflict as error:
        raise IntakeBindingConflict(['sourceUnavailable']) from error
    differing=[]
    if photo!=item['photoId']:differing.append('photoId')
    if revision!=item['processingRevision']:differing.append('processingRevision')
    if source.get('fileHash')!=data['sha256']:differing.append('sourceHash')
    if data.get('blobName') and data['blobName']!=photo:differing.append('blobName')
    if differing:raise IntakeBindingConflict(differing)
    # Use the committed filename and policy, even if filename resolution or
    # the default processing policy has changed since the original request.
    return dict(complete=True,filename=item['acceptedFilename'],duplicates=[],
        uploadId=data.get('uploadId') or '',bytesReceived=data.get('totalSize',0),
        totalSize=data.get('totalSize',0),clientProcessingLateResultWaitSeconds=0,
        processingIsolated=source.get('processingIsolated') is True)


def _binding_conflict_response(app,error):
    app.app.logger.warning('Wave finalize binding conflict fields=%s',','.join(error.fields))
    return app.jsonify(error=str(error),code='wave_binding_conflict',fields=error.fields),409


def install(blueprint):
    import app
    @blueprint.route('/upload/waves/protocol',methods=['GET'])
    def upload_wave_protocol():
        if os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','').lower()=='true':
            return app.jsonify(error='Wave deployment bootstrap is pending'),503
        library,error=app._require_user_id(require_auth=True)
        if error:return error
        if app.jobs_table_client is None:return app.jsonify(error='Wave policy storage unavailable'),503
        try:
            response=app.jsonify(protocol='live' if wave_policy.owned(app.jobs_table_client,library) else 'legacy')
            response.headers['Cache-Control']='no-store'
            return response
        except AzureError:return app.jsonify(error='Wave policy storage unavailable'),503

    @blueprint.before_request
    def wave_upload_guard():
        path=app.request.path.rstrip('/')
        operation=path.rsplit('/',1)[-1]
        if os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','').lower()=='true':
            return app.jsonify(error='Wave deployment bootstrap is pending'),503
        if operation not in ('init','init-batch','finalize','finalize-batch','claim','claim-batch','client-processing'):
            return
        library,error=app._require_user_id()
        if error or not library:return
        try:
            if not wave_policy.owned(app.jobs_table_client,library):
                if (os.getenv('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED','').lower()=='true'
                        and app.library_store is not None
                        and app.library_store.get_library(library) is None):
                    return app.jsonify(error='Library bootstrap is incomplete'),503
                return
            if not wave_policy.configuration_ready():raise Conflict('Wave source capabilities are unavailable')
            store=ShadowWaves(app.jobs_table_client,library)
            wave_policy.require_capability(store,'upload')
            if store._required('policy').get('cleanupState'):raise Conflict('Library cleanup is in progress')
            if operation in ('claim','claim-batch','client-processing'):
                raise Conflict('Vision owns processing for this library')
            data=app.request.get_json(silent=True) or {}
            files=data.get('files') if operation.endswith('-batch') else [data]
            if not isinstance(files,list) or not files:raise ValueError('Upload reservations required')
            bindings=[wave_policy.validate_admission(store,item,finalizing=True) for item in files]
            if operation.startswith('finalize'):
                g.wave_finalize_replays={binding:replay for binding,item in zip(bindings,files)
                    if (replay:=_verified_replay(store,binding,item,app.metadata_table_client)) is not None}
            g.wave_upload=(store,bindings,operation)
            g.wave_source_token=wave_policy.begin_source_change()
            g.wave_finalize_token=wave_policy.begin_finalization(lambda:wave_policy.check_finalization(store,bindings))
        except IntakeBindingConflict as error:
            return _binding_conflict_response(app,error)
        except (Conflict,ValueError,KeyError,TypeError) as error:
            return app.jsonify(error=str(error),code='wave_admission_required'),409
        except AzureError:
            return app.jsonify(error='Wave policy storage unavailable'),503

    @blueprint.after_request
    def bind_finalized_sources(response):
        context=getattr(g,'wave_upload',None)
        if not context or response.status_code>=400 or not context[2].startswith('finalize'):return response
        store,bindings,_=context
        result=response.get_json(silent=True) or {}
        results=result.get('results',[]) if context[2].endswith('-batch') else [result]
        try:
            from upload_wave_shadow import resolve_source
            from routes.upload_wave_shadow import close_resolved_intake
            for binding,entry in zip(bindings,results):
                if entry.get('error') or not entry.get('complete'):continue
                # Finalization wrote this source's metadata, so it requires
                # publication even if dedup finds identical bytes (including
                # itself on a retry). No-write duplicate selections use the
                # explicit resolve endpoint instead.
                if binding in getattr(g,'wave_finalize_replays',{}):
                    session=store._required(store.session_key(binding[0]))
                else:
                    session=resolve_source(store,app.metadata_table_client,*binding,
                        'accepted',entry['filename'])
                session=close_resolved_intake(store,session)
                entry['waveResolution']=dict(sessionId=binding[0],itemId=binding[1],
                    manifestRevision=session['manifestRevision'],state=session['state'])
            response.set_data(app.jsonify(result).get_data())
        except IntakeBindingConflict as error:
            body,status=_binding_conflict_response(app,error)
            body.status_code=status
            return body
        except (AzureError,Conflict,ValueError):
            app.app.logger.exception('Wave finalize admission must be retried')
            # after_request hooks must return a Response, unlike route handlers.
            retry=app.jsonify(error='Wave source admission requires retry',code='wave_admission_retry')
            retry.status_code=503
            retry.headers['Retry-After']='1'
            return retry
        return response

    @blueprint.teardown_request
    def end_source_request(error):
        finalization=getattr(g,'wave_finalize_token',None)
        if finalization is not None:
            wave_policy.end_finalization(finalization)
            g.wave_finalize_token=None
        token=getattr(g,'wave_source_token',None)
        if token is not None:
            wave_policy.end_source_change(token)
            g.wave_source_token=None
