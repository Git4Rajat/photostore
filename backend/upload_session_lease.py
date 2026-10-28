"""Renewable intake leases and durable, delayed expiry checks.

A check is queued BEFORE a leased session can be committed. Renewals only
update that session; a check reschedules itself from the authoritative expiry.
Duplicate checks and deliveries are safe under the existing ETag fences.
"""
import json
import math
from azure.core.exceptions import AzureError, ResourceNotFoundError
from upload_waves import Conflict, ShadowWaves

LEASE_SECONDS = 300
QUEUE_NAME = 'photostore-upload-expiry'
TYPE = 'upload_session_expiry'


class ExpiryUnavailable(AzureError):
    """The durable expiry wake cannot be stored; admission must not proceed."""


def send_check(library, session, delay=LEASE_SECONDS):
    import app
    if app.queue_service_client is None:
        raise ExpiryUnavailable('Upload expiry queue unavailable')
    queue = app.queue_service_client.get_queue_client(QUEUE_NAME)
    # Provisioned in IaC; never acknowledge admission without a durable wake.
    try:
        queue.send_message(json.dumps(dict(type=TYPE, userId=library, sessionId=session)),
                           visibility_timeout=max(0, math.ceil(delay)), time_to_live=-1)
    except ResourceNotFoundError as error:
        raise ExpiryUnavailable('Upload expiry queue is not provisioned') from error


def active(parent, now):
    return (parent['state'] == 'open' and not parent.get('abandoning')
            and (not parent.get('leaseExpiresAt') or parent['leaseExpiresAt'] > now))


def renew(store, session):
    def commit():
        old = store._required(store.session_key(session))
        if not active(old, store.clock()):
            return dict(old, leaseActive=False)
        # Upgrade old browser sessions safely: queue first, then enable expiry.
        if not old.get('leaseExpiresAt'):
            send_check(store.library, session)
        row = dict(old, heartbeatAt=store.clock(), leaseExpiresAt=store.clock()+LEASE_SECONDS)
        store.table.submit_transaction([store._change(old, row), *store._policy_guard()])
        return dict(row, leaseActive=True)
    return store._retry(commit)


def seal_expired(store, session):
    """Heartbeat and expiry race on the same ETag. Expiry cannot be resurrected.

    Detach collecting intake before cancellation so a resumed selection gets a
    successor even while the old parent's finalizations are still draining.
    """
    def commit():
        old = store._get(store.session_key(session))
        if not old or old['state'] != 'open' or not old.get('leaseExpiresAt'):
            return old
        if old['leaseExpiresAt'] > store.clock() or old.get('abandoning'):
            return old
        control = store._required('control')
        row = dict(old, abandoning=True, abandonReason='Upload heartbeat lease expired',
                   leaseExpiredAt=store.clock())
        store._commit(control, [store._change(old, row)], control_updates=(
            {'collectingWaveId': ''} if control.get('collectingWaveId') == old['waveId'] else None))
        return row
    return store._retry(commit)


def expire(store, session, send, drive, emit=lambda **fields: None):
    parent = seal_expired(store, session)
    if not parent or parent.get('discardAbsent'):
        # A pre-admission wake can outlive a failed create. No intake to close.
        return
    if parent['state'] != 'open':
        # Redelivery also repairs a crash between close and stage handoff.
        drive(store, parent['waveId'])
        return
    if not parent.get('leaseExpiresAt'):
        return  # An old, unleased session has no deadline to enforce.
    if not parent.get('abandoning'):
        send(store.library, session, max(1, parent['leaseExpiresAt']-store.clock()))
        return
    from wave_settlement import abandon
    # Drain bounded storage pages in one delivery. Requeue only at the budget
    # boundary, rather than paying a queue round trip/poll delay per 20 files.
    for _ in range(20):
        try:
            result = abandon(store, session, parent['manifestRevision'], reason=parent['abandonReason'])
        except Conflict:
            # Finalization can still own a bounded lease, or win the manifest
            # race. Preserve it and reload authoritative state next time.
            send(store.library, session, 30)
            emit(event='upload_session_expiry_deferred', wave_id=parent['waveId'])
            return
        if result['state'] != 'open':
            drive(store, result['waveId'])
            emit(event='upload_session_expired', wave_id=result['waveId'],
                 registered=result['registeredCount'], resolved=result['resolvedCount'],
                 cancelled=result.get('abandonCancelledCount',0),
                 expiry_lag_seconds=max(0, store.clock()-parent['leaseExpiresAt']))
            return
        parent = result
    send(store.library, session, 0)


def consume(payload):
    import app
    import processing_diagnostics
    from wave_policy import owned
    from routes.upload_wave_shadow import _drive
    library, session = payload['userId'], payload['sessionId']
    if not owned(app.jobs_table_client, library):
        return
    expire(ShadowWaves(app.jobs_table_client, library), session, send_check, _drive,
           emit=lambda **fields: processing_diagnostics.emit(fields.pop('event'), **fields))


def start_worker(queue_service, shutdown, poll_seconds, poll, logger):
    """Poll independently of clustering's potentially long synchronous jobs."""
    import threading
    queue=queue_service.get_queue_client(QUEUE_NAME)
    def run():
        while not shutdown.is_set():
            try:
                # Expiry checks must survive transient outages indefinitely.
                # The durable session state makes duplicate deliveries safe.
                if poll(queue,QUEUE_NAME,float('inf'),None):continue
            except Exception:
                logger.exception('Upload session expiry poll failed')
            shutdown.wait(poll_seconds)
    thread=threading.Thread(target=run,name='upload-expiry',daemon=True)
    thread.start()
    return thread
