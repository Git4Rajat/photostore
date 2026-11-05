"""Authoritative upload activity and ETag fencing; no expiry queue or poller.

Core discovers and reconciles sessions through the lifecycle table when the user
returns. This module only renews activity and fences stale intake.
"""
LEASE_SECONDS = 300


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
