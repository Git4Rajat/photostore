"""Opt-in real dispatch for upload_waves.py's shadow outbox rows.

Disabled by default (UPLOAD_WAVE_EXECUTION_ENABLED). Reuses index_work.
WorkStore for durable claim/lease/renew/release/recover-on-expiry rather than
inventing new lease machinery -- see docs/indexing-clustering-target-
state.md step 4 ("wave execution").

Scope of this module today: durably *requesting* work for an existing
outbox row (index_work.WorkStore.request persists the intent) and
promoting a `waiting_predecessor` successor once its predecessor is ready.
It deliberately does NOT call WorkStore.notify()/recover() to wake a real
queue consumer -- there is no Vision/cluster wave worker to wake yet (that
is step 5, one-shot clustering, and the equivalent Vision-side work). Wiring
request() through to an actual queue wakeup is follow-up work once that
consumer exists; until then, a durable, unclaimed WorkStore row is the
correct and complete state, not a bug.
"""
import os

MAX_SWEEP_PAGES = 50


def execution_enabled():
    return os.getenv('UPLOAD_WAVE_EXECUTION_ENABLED', '').strip().lower() == 'true'


def dispatch_outbox(work_store, store, library, outbox_key, destination):
    """Durably requests real work for an existing shadow outbox row, then
    marks it dispatchable. Returns True only the first time it actually
    does that for this outbox -- False when execution is disabled, the
    outbox row doesn't exist yet, or it was already dispatched (so a caller
    like sweep_library can tell "newly dispatched" from "nothing to do").
    """
    if not execution_enabled():
        return False
    outbox = store._get(outbox_key)
    if outbox is None or outbox.get('dispatchable'):
        return False
    scope = f'wave-{destination}:{outbox["waveId"]}'
    work_store.request(library, scope, advance=False)
    store.set_dispatchable(outbox_key, dispatchable=True)
    return True


def advance_wave(work_store, store, library, wave_id):
    """Single-wave driver: promotes out of waiting_predecessor if eligible,
    then dispatches whichever outbox the wave's current state makes
    eligible (visionPendingKey while processing_pending, the deterministic
    shadow-cluster! outbox once ready). Safe to call on any wave in any
    state -- every branch is a no-op when not applicable.
    """
    wave = store.promote_successor(wave_id)
    dispatched = []
    if wave['state'] == 'processing_pending' and wave.get('visionPendingKey'):
        if dispatch_outbox(work_store, store, library, wave['visionPendingKey'], 'vision'):
            dispatched.append('vision')
    if wave['state'] == 'ready':
        if dispatch_outbox(work_store, store, library, 'shadow-cluster!' + wave_id, 'cluster'):
            dispatched.append('cluster')
    return wave, dispatched


def sweep_library(work_store, store, library, *, limit=MAX_SWEEP_PAGES):
    """Bounded scan over this library's wave rows, advancing each one.

    Serves both roles the target doc asks for: an opportunistic call right
    after a wave transition, and the independent recovery sweep for crash
    recovery (re-running this is always safe -- every step it takes is
    idempotent). Returns the waves it touched for diagnostics/tests.
    """
    touched = []
    after = ''
    for _ in range(limit):
        page = store._page('wave!', after)
        if not page:
            break
        for row in page:
            wave, dispatched = advance_wave(work_store, store, library, row['waveId'])
            if dispatched or wave['state'] != row['state']:
                touched.append({'waveId': row['waveId'], 'state': wave['state'], 'dispatched': dispatched})
        after = page[-1]['RowKey']
        if len(page) < 20:
            break
    return touched
