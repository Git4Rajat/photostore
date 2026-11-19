"""Explicit opt-in shadow intake API; intentionally no automatic dispatch."""
from functools import wraps
import json
import os

from azure.core.exceptions import AzureError, ResourceNotFoundError

import app
from upload_waves import ArtifactCleanupBusy, Conflict, MutationContention, ShadowWaves
import upload_wave_shadow as source

MAX_MANIFEST_REQUEST = 2000
MAX_RESOLVE_REQUEST = 100


def _public(row):
    result = {key: value for key, value in row.items()
              if key not in ('PartitionKey', 'RowKey', 'Timestamp', 'evaluation')}
    if 'evaluation' in row:
        progress = json.loads(row.get('evaluation') or '{}')
        result.update(scanInProgress=bool(progress) and row.get('state') != 'ready',
                      scannedMembers=progress.get('members', 0),
                      scannedSessions=progress.get('sessions', 0))
    return result


def _find_stored(store):
    # Older deployments without the hash index keep the previous behavior.
    if app.metadata_table_client is None or app.hash_index_table_client is None:
        return None
    return source.stored_filename_lookup(app.hash_index_table_client, app.metadata_table_client, store.library)


def stored_reservation_adopter(store):
    """abandon(adopt=...) for this library: adopt reservations already stored."""
    find_stored = _find_stored(store)
    if find_stored is None:
        return None
    return lambda session, reservations: source.adopt_stored_reservations(
        store, app.metadata_table_client, session, reservations, find_stored)


def endpoint(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        live='/waves/live/' in app.request.path
        if not live and os.getenv('UPLOAD_WAVE_SHADOW_ENABLED', '').strip().lower() != 'true':
            return app.jsonify({'error': 'Shadow upload protocol is disabled'}), 404
        _, library, error = app._require_library_context(require_auth=True)
        if error:
            return error
        # Large manifests are a single browser request; storage transactions
        # remain bounded independently inside the registration handler.
        operation = app.request.path.rsplit('/', 1)[-1]
        limit = {'items': 16 * 1024 * 1024, 'resolve-batch': 1024 * 1024}.get(operation, 65536)
        if app.request.content_length and app.request.content_length > limit:
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
                policy=store._required('policy')
                if policy.get('cleanupState'):raise Conflict('Library cleanup is in progress')
                # Check before admission writes its directory row. Atomic mutation guards
                # still catch GC starting after this inexpensive preflight.
                if app.request.method=='POST' and policy.get('gcUntil',0)>store.clock():
                    raise ArtifactCleanupBusy('Upload is waiting for background cleanup to finish.')
            result = function(store, data, *args, **kwargs)
            response = app.jsonify(dict(_public(result), protocolMode='live' if live else 'shadow', dispatchable=False, evidenceQualified=False))
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ResourceNotFoundError:
            return app.jsonify({'error':'Upload session state not found',
                                'code':'wave_state_not_found'}),409
        except ArtifactCleanupBusy as error:
            import processing_diagnostics
            from upload_waves import digest
            processing_diagnostics.emit('upload_wave_deferred',library_id=digest(library),
                reason='artifact_cleanup',operation=operation,retry_after_seconds=2)
            response=app.jsonify({'error':str(error),'code':'wave_artifact_cleanup_busy','retryAfterSeconds':2})
            response.headers['Retry-After']='2'
            return response,409
        except MutationContention as error:
            response=app.jsonify({'error':str(error),'code':'wave_mutation_contention'})
            response.headers['Retry-After']='1'
            return response,409
        except Conflict as error:
            return app.jsonify({'error': str(error)}), 409
        except (ValueError, KeyError, TypeError):
            return app.jsonify({'error': 'Invalid shadow protocol request'}), 400
        except AzureError:
            app.app.logger.exception('Shadow upload protocol storage failure')
            return app.jsonify({'error': 'Shadow protocol storage unavailable'}), 503
    return wrapped


FAILURES_RETRIED_PREFIX='failures-retried!'
RETRY_FAILED_PAGE=20


def _retry_failed_members(store,wave,after):
    """Force a fresh processing generation for one page of members that gave up.

    Only the generation that failed is retried: a photo since deleted, replaced
    or already retried keeps its current state. The last page records a marker
    so the Tools page stops offering the retry for this wave.
    """
    from processing_generation import REQUIRED_STEPS
    prefix=f'e!{wave}!m!'
    if not isinstance(after,str) or (after and not after.startswith(prefix)):
        raise ValueError('Invalid retry cursor')
    store.status(wave)  # Validates the wave belongs to this library.
    retried=skipped=failed=0
    rows=store._page(prefix,after,size=RETRY_FAILED_PAGE)
    for member in rows:
        after=member['RowKey']
        if member.get('outcome')!='failed_exhausted':
            continue
        try:
            row=app.metadata_table_client.get_entity(store.library,member['filename'])
        except ResourceNotFoundError:
            skipped+=1;continue
        if (row.get('processing_state')=='deleted' or (row.get('anonymousImageId'),row.get('processingRevision'))
                !=(member['photoId'],member['processingRevision'])):
            skipped+=1;continue
        result=app._enqueue_processing_steps(store.library,member['filename'],list(REQUIRED_STEPS),force=True)
        if any(value.get('status')=='error' for value in result.values()):failed+=1
        else:retried+=1
    finished=len(rows)<RETRY_FAILED_PAGE
    if finished and not failed:
        def mark():
            old=store._get(FAILURES_RETRIED_PREFIX+wave)
            store.table.submit_transaction([store._change(old,store._row(FAILURES_RETRIED_PREFIX+wave,
                waveId=wave,retriedAt=store.clock())),*store._policy_guard()])
        store._retry(mark)
    return dict(retried=retried,skipped=skipped,failedRetries=failed,after='' if finished else after)


def register_routes(blueprint):
    @blueprint.route('/upload/waves/live/sessions', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions', methods=['POST'])
    @endpoint
    def wave_shadow_open(store, data):
        from upload_session_lease import LEASE_SECONDS, seal_expired
        from upload_session_registry import Sessions, configured
        live='/waves/live/' in app.request.path
        existing=store._get(store.session_key(data['sessionId']))
        if live and (existing is None or existing['state']=='open'):
            Sessions(configured(), store.library, clock=store.clock).track(data['sessionId'])
        if live:
            if data.get('resumedFromSessionId'):
                seal_expired(store, data['resumedFromSessionId'])
            if existing:
                seal_expired(store, data['sessionId'])
        result=store.open_session(data['sessionId'], data['declaredCount'],
            resumed_from=data.get('resumedFromSessionId', ''), lease_seconds=LEASE_SECONDS if live else 0, lifecycle_managed=live)
        from upload_session_lease import active
        return dict(result,leaseActive=active(result,store.clock()),serverNow=store.clock())

    @blueprint.route('/upload/waves/live/sessions/<session>/items', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/items', methods=['POST'])
    @endpoint
    def wave_shadow_register(store, data, session):
        from upload_waves import REGISTER_MAX_PAGE, identifier
        items = data['items']
        if not isinstance(items, list) or not 0 < len(items) <= MAX_MANIFEST_REQUEST:
            raise ValueError('Invalid manifest size')
        # Validate the whole request before committing its first storage page.
        ids = []
        for item in items:
            ids.append(identifier(item['itemId']))
            if not isinstance(item.get('filename'), str) or not 0 < len(item['filename']) <= 1024:
                raise ValueError('Invalid filename')
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate itemId')
        for offset in range(0, len(items), REGISTER_MAX_PAGE):
            result = store.register(session, items[offset:offset + REGISTER_MAX_PAGE])
        return result

    @blueprint.route('/upload/waves/live/sessions/<session>/resolve-batch', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/resolve-batch', methods=['POST'])
    @endpoint
    def wave_shadow_resolve_batch(store, data, session):
        from upload_waves import identifier
        items = data['items']
        if not isinstance(items, list) or not 0 < len(items) <= MAX_RESOLVE_REQUEST:
            raise ValueError('Invalid outcome batch size')
        ids = []
        for item in items:
            ids.append(identifier(item['itemId']))
            if item['outcome'] not in ('accepted', 'duplicate', 'cancelled', 'failed'):
                raise ValueError('Invalid outcome')
            if item['outcome'] in ('accepted', 'duplicate'):
                if app.metadata_table_client is None:
                    raise AzureError('Metadata storage unavailable')
                filename = item.get('filename', '')
                if not isinstance(filename, str) or not app._validate_media_filename(filename):
                    raise ValueError('Finalized filename required')
                item['filename'] = app._validate_media_filename(filename)
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate itemId')
        # Partial commits are safe to replay after a lost response or failure.
        # Acknowledge only when every item has been durably resolved.
        find_stored = _find_stored(store)
        adopted = []
        for item in items:
            result = source.resolve_sources(store, app.metadata_table_client, session,
                [(item['itemId'], item['outcome'], item.get('filename', ''))], find_stored=find_stored)
            adopted.extend(result.get('adoptedItems', ()))
        result = close_resolved_intake(store, result) if '/waves/live/' in app.request.path else result
        return dict(result, adoptedItems=adopted) if adopted else result

    @blueprint.route('/upload/waves/live/sessions/<session>/resolve', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/resolve', methods=['POST'])
    @endpoint
    def wave_shadow_resolve(store, data, session):
        if app.metadata_table_client is None and data['outcome'] in ('accepted', 'duplicate'):
            raise AzureError('Metadata storage unavailable')
        filename = app._validate_media_filename(data.get('filename', ''))
        if data['outcome'] in ('accepted', 'duplicate') and not filename:
            raise ValueError('Finalized filename required')
        result=source.resolve_sources(store, app.metadata_table_client, session,
                                      [(data['itemId'], data['outcome'], filename)], find_stored=_find_stored(store))
        adopted=result.get('adoptedItems')
        result=close_resolved_intake(store,result) if '/waves/live/' in app.request.path else result
        return dict(result,adoptedItems=adopted) if adopted else result

    @blueprint.route('/upload/waves/live/sessions/<session>/close', methods=['POST'])
    @blueprint.route('/upload/waves/shadow/sessions/<session>/close', methods=['POST'])
    @endpoint
    def wave_shadow_close(store, data, session):
        # Explicit resolution is required even for stopped/failed selections.
        result=store.close(session, data['manifestRevision'], data['closeId'])
        if '/waves/live/' in app.request.path:
            _drive(store,result['waveId'])
            from upload_session_registry import finish
            finish(store, result)
        return result

    @blueprint.route('/upload/waves/live/sessions/<session>/discard', methods=['POST'])
    @blueprint.route('/upload/waves/live/sessions/<session>/abandon', methods=['POST'])
    @endpoint
    def wave_live_abandon(store,data,session):
        account,_,error=app._require_library_context(require_auth=True)
        if error:return error
        # Any authenticated library member may stop an intake session they hold
        # the (unguessable) id of; abandonment only cancels that session's own
        # unresolved reservations. Resolved sources remain library members.
        from wave_settlement import abandon, fence_discard
        reason=data['reason']
        if not isinstance(reason,str) or not reason.strip() or len(reason)>256:
            raise ValueError('Abandonment reason required')
        modern=app.request.path.endswith('/discard')
        parent=store._get(store.session_key(session))
        if modern or parent is None or parent.get('discardAbsent'):
            parent=fence_discard(store,session,reason=reason,
                                 resumed_from=data.get('resumedFromSessionId',''))
        if parent.get('discardAbsent'):
            return dict(parent,state='absent')
        def release_tracking(filenames):
            for name in filenames:
                app._cleanup_failed_upload(store.library,name)
        result=abandon(store,session,data.get('manifestRevision',parent['manifestRevision']),reason=reason,
                       on_cancelled=release_tracking,adopt=stored_reservation_adopter(store))
        _drive(store,result['waveId'])
        from upload_session_registry import finish
        finish(store, result)
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
            from wave_retry import retry_stage
            retry_stage(store,wave,current)
        elif action=='retry-failed':
            result=_retry_failed_members(store,wave,data.get('after',''))
            _drive(store,wave)
            return dict(store.status(wave),**result)
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
        from wave_health import status
        return status(store, wave)

    @blueprint.route('/upload/waves/live/attention', methods=['GET'])
    @endpoint
    def wave_attention(store, data):
        """Unfinished waves (not collecting, complete or cancelled) with their health, so the Tools page can
        show which ones are failed or blocked and offer a stage retry, plus finished waves whose failed photos
        have not been retried yet. Bounded: a few pages, 25 results."""
        from wave_health import status
        waves, after = [], ''
        for _ in range(10):
            rows = store._page('wave!', after)
            if not rows:
                break
            after = rows[-1]['RowKey']
            for row in rows:
                wave_id = row['RowKey'][len('wave!'):]
                if (row.get('state') == 'complete' and int(row.get('failedMembers') or 0)
                        and store._get(FAILURES_RETRIED_PREFIX + wave_id) is None):
                    # Finished, but some photos gave up: offer to retry them.
                    waves.append(dict(waveId=wave_id, state='complete', failedMembers=int(row['failedMembers'])))
                elif row.get('state') in ('collecting', 'complete', 'cancelled'):
                    continue
                else:
                    waves.append(_public(status(store, wave_id)))
                if len(waves) >= 25:
                    return dict(waves=waves, truncated=True)
        return dict(waves=waves, truncated=False)

    @blueprint.route('/upload/waves/live/retry-stuck', methods=['POST'])
    @endpoint
    def wave_retry_stuck(store, data):
        """The processing bar's Retry: re-queue every stuck wave that is not itself waiting on an
        earlier one, oldest first. Same repair as the Tools card, chosen on the server."""
        account,_,error=app._require_library_context(require_auth=True)
        if error or not app.library_store.is_owner(account,store.library):
            raise Conflict('Only the library owner may repair a wave')
        from wave_retry import retry_stuck
        return retry_stuck(store,lambda wave:_drive(store,wave))

    @blueprint.route('/upload/waves/live/<wave>/failures', methods=['GET'])
    @endpoint
    def wave_failure_review(store, data, wave):
        if app.metadata_table_client is None:
            raise AzureError('Metadata storage unavailable')
        from wave_failure_review import page
        return page(store, app.metadata_table_client, wave, after=app.request.args.get('after', ''))


# Durable, cross-replica-safe throttle for the opportunistic recovery sweep
# below. A plain in-process dict was tried first and rejected: this app runs
# multiple replicas/workers, each with its own independent memory, so an
# in-process cooldown doesn't actually bound the cluster-wide sweep rate and
# resets to zero on every restart or scale event. This claim row lives in the
# same table as everything else wave-related, guarded by the existing
# optimistic-concurrency (_change/ETag) pattern, so only one caller anywhere
# in the fleet wins the claim for a given library in a given window.
_RECOVERY_SWEEP_INTERVAL_SECONDS = 120


def _claim_recovery_nudge(store):
    """True if this caller should run the sweep now; False if another request
    (this replica or another) already claimed the current window, or just
    finished one. Best-effort: losing the race or the write is never an
    error, since recovery is advisory and the next real event tries again."""
    from azure.data.tables import TableTransactionError
    key = 'recovery-nudge'
    now = store.clock()
    current = store._get(key)
    if current and float(current.get('nextEligibleAt') or 0) > now:
        return False
    row = store._row(key, nextEligibleAt=now + _RECOVERY_SWEEP_INTERVAL_SECONDS)
    try:
        store.table.submit_transaction([store._change(current, row)])
        return True
    except TableTransactionError as error:
        if error.status_code in (409, 412):
            return False
        raise


def _maybe_recover_stuck_stages(store):
    # Opportunistic repair reduces handoff latency. The bounded scheduled
    # recovery job also repairs libraries with no active browser.
    if not _claim_recovery_nudge(store):
        return
    import processing_diagnostics
    import processing_cleanup
    from wave_recovery import recover_library
    from wave_runtime import send_stage
    try:
        recover_library(store.table, store.library, send_stage, wave_pages=1,
            release_limit=20, clock=store.clock, metadata=app.metadata_table_client,
            cleanup_send=processing_cleanup.send_artifact_cleanup,
            emit=lambda **fields: processing_diagnostics.emit(fields.pop('event'), **fields))
    except Exception:
        app.app.logger.exception('Opportunistic wave-stage recovery sweep failed for %s', store.library)


def _drive(store,wave_id):
    from upload_wave_execution import advance_wave,stage_work_store,release_and_relay,execution_enabled
    if not execution_enabled():return False
    from wave_runtime import send_stage
    work=stage_work_store(store.table)
    wave,_=advance_wave(work,store,store.library,wave_id)
    state=wave['state']
    destination=('vision' if state in ('processing_pending','processing') else
        'cluster' if state=='ready' else 'index' if state=='indexing' else None)
    key=(wave.get('visionPendingKey') if destination=='vision' else
         'shadow-cluster!'+wave_id if destination=='cluster' else wave.get('indexPendingKey'))
    if destination and key:
        # A lifecycle directory entry is a retained recovery receipt. An old
        # notify reservation alone cannot prove its queue send actually landed.
        release_and_relay(work,store,store.library,key,destination,send_stage,require_delivery=True)
    if state in ('complete', 'cancelled'):
        from wave_runtime import relay_successor, resume_completed_wave_maintenance
        relay_successor(work,store,wave_id,send_stage,require_delivery=True)
        resume_completed_wave_maintenance(store.library)
    # Piggyback recovery of any OTHER stage this library left stuck (e.g. a
    # prior wave whose wake got lost to a worker scale-down race) on this
    # genuine event; independent recovery covers browser and worker loss.
    _maybe_recover_stuck_stages(store)
    return True


def close_resolved_intake(store,session):
    if (session['state']=='open' and session['registeredCount']==session['declaredCount']
            and session['resolvedCount']==session['declaredCount']):
        session=store.close(session['sessionId'],session['manifestRevision'],'auto-complete')
    if session['state'] != 'open':
        # A finalize replay can observe the committed close after a failed
        # handoff. Replay the handoff before removing its recovery directory.
        _drive(store,session['waveId'])
        from upload_session_registry import finish
        finish(store, session)
    return session
