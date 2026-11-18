"""Advisory index-ready push; durable jobs/polling remain the recovery path."""
import hashlib
import logging
import os
import threading
import time
from functools import lru_cache

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_pending = {}
_running = False
# Free_F1 allows 20,000 messages/day across every sender. Index-ready pushes keep
# most of that; processing pushes (one per finished wave, so independent of photo
# count) get a small fixed share per sending process, after which clients simply
# fall back to polling.
PROCESSING_DAILY_BUDGET = int(os.getenv('INDEX_EVENTS_PROCESSING_DAILY_BUDGET', '1500'))
_budget = {'day': None, 'used': 0}
# Progress steps (about 15 per wave) are the cheapest thing to drop: past the share
# or inside the per-library gap, clients simply keep their polling fallback.
PROGRESS_DAILY_BUDGET = int(os.getenv('INDEX_EVENTS_PROGRESS_DAILY_BUDGET', '3000'))
PROGRESS_MIN_GAP_SECONDS = float(os.getenv('INDEX_EVENTS_PROGRESS_MIN_GAP_SECONDS', '30'))
_progress = {'day': None, 'used': 0, 'last': {}}


def enabled():
    return bool(os.getenv('INDEX_EVENTS_ENDPOINT', '').strip())


def group(library):
    return 'library_' + hashlib.sha256(library.encode()).hexdigest()


@lru_cache(maxsize=1)
def _client():
    from azure.identity import DefaultAzureCredential
    from azure.messaging.webpubsubservice import WebPubSubServiceClient
    return WebPubSubServiceClient(
        endpoint=os.environ['INDEX_EVENTS_ENDPOINT'], hub='index_updates',
        credential=DefaultAzureCredential(), retry_total=0,
        connection_timeout=3, read_timeout=3,
    )


def negotiate(library):
    if not enabled():
        return {'available': False}
    token = _client().get_client_access_token(
        groups=[group(library)], roles=[], minutes_to_expire=5)
    return {'available': True, 'url': token['url'], 'libraryId': library}


def publish(library, scope, job_id):
    """Coalesce sends without holding up publication or spawning per-edit threads."""
    _enqueue((library, scope), {'type': 'index-ready', 'libraryId': library, 'scope': scope, 'jobId': job_id})


def publish_processing(library, wave_id, state='done'):
    """Advisory "a wave finished processing" for browsers watching an upload.

    One message per wave transition, coalesced per (library, wave); never per
    photo, so a 16k-file upload costs a handful of messages. Past the daily share
    the push is skipped and browsers keep their slow polling fallback."""
    if not enabled():
        return
    today = time.strftime('%Y-%m-%d', time.gmtime())
    with _lock:
        if _budget['day'] != today:
            _budget.update(day=today, used=0)
        if (library, 'processing', wave_id) not in _pending:
            if _budget['used'] >= PROCESSING_DAILY_BUDGET:
                return
            _budget['used'] += 1
    _enqueue((library, 'processing', wave_id), {
        'type': 'processing', 'libraryId': library, 'waveId': wave_id, 'state': state})


def publish_progress(library, wave_id, stage, step=None, eta_seconds=None, *, force=False):
    """Advisory step/stage change for a wave's progress bar; the GET stays authoritative.

    `force` (stage changes) skips the per-library gap but never the daily budget."""
    if not enabled() or os.getenv('PROCESSING_PROGRESS_PUSH', 'true').strip().lower() in ('0', 'false', 'no', 'off'):
        return False
    now = time.time()
    today = time.strftime('%Y-%m-%d', time.gmtime(now))
    with _lock:
        if _progress['day'] != today:
            _progress.update(day=today, used=0, last={})
        if _progress['used'] >= PROGRESS_DAILY_BUDGET:
            return False
        if not force and now - _progress['last'].get(library, 0) < PROGRESS_MIN_GAP_SECONDS:
            return False
        _progress['used'] += 1
        _progress['last'][library] = now
    _enqueue((library, 'progress', wave_id), {
        'type': 'processing-progress', 'libraryId': library, 'waveId': wave_id,
        'stage': stage, 'step': step, 'etaSeconds': eta_seconds})
    return True


def _enqueue(key, message):
    if not enabled():
        return
    global _running
    with _lock:
        _pending[key] = message
        if _running:
            return
        _running = True
    threading.Thread(target=_flush, name='index-ready-push', daemon=True).start()


def _flush():
    global _running
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            (library, *_), message = _pending.popitem()
        try:
            _client().send_to_group(group(library), message, content_type='application/json')
        except Exception:
            _log.warning('Index-ready push failed; clients can recover through job polling', exc_info=True)
