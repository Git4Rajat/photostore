"""Authoritative upload activity and ETag fencing; no expiry queue or poller.

Core discovers sessions through the lifecycle table when the user returns.
The expiry helper remains usable by bounded operator recovery.
"""
from azure.core.exceptions import AzureError
from upload_waves import Conflict

LEASE_SECONDS = 300
TYPE = 'upload_session_expiry'


class ExpiryUnavailable(AzureError):
    """Compatibility error for older clients during rolling upgrades."""


def send_check(*args, **kwargs):
    """Compatibility no-op: no producer may wake clustering for upload expiry."""


def start_worker(*args, **kwargs):
    """Compatibility no-op for deployments/tests upgrading the old worker."""


def active(parent, now):
    return (parent['state'] == 'open' and not parent.get('abandoning')
            and (not parent.get('leaseExpiresAt') or parent['leaseExpiresAt'] > now))


def renew(store, session):
    def commit():
        old = store._required(store.session_key(session))
        if not active(old, store.clock()):
            return dict(old, leaseActive=False)
        row = dict(old, heartbeatAt=store.clock(), lastRequestAt=store.clock(), leaseExpiresAt=store.clock()+LEASE_SECONDS)
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
        if not old or old['state'] != 'open':
            return old
        deadline = old.get('leaseExpiresAt') or max(old.get('lastRequestAt', 0), old.get('heartbeatAt', 0), old.get('createdAt', 0)) + LEASE_SECONDS
        if deadline > store.clock() or old.get('abandoning'):
            return old
        control = store._required('control')
        row = dict(old, abandoning=True, abandonReason='Upload heartbeat lease expired',
                   leaseExpiredAt=store.clock(), discardRequestedAt=store.clock())
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
