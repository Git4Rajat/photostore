"""Authoritative admission fences for libraries born into the wave protocol."""
from flask import g
import os
from azure.core.exceptions import AzureError
from upload_waves import ShadowWaves,Conflict
import wave_policy


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
            g.wave_upload=(store,bindings,operation)
            g.wave_source_token=wave_policy.begin_source_change()
            g.wave_finalize_token=wave_policy.begin_finalization(lambda:wave_policy.check_finalization(store,bindings))
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
                session=resolve_source(store,app.metadata_table_client,*binding,
                    'accepted',entry['filename'])
                session=close_resolved_intake(store,session)
                entry['waveResolution']=dict(sessionId=binding[0],itemId=binding[1],
                    manifestRevision=session['manifestRevision'],state=session['state'])
            response.set_data(app.jsonify(result).get_data())
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
