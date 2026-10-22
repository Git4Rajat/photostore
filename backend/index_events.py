"""Advisory index-ready push; durable jobs/polling remain the recovery path."""
import hashlib
import logging
import os
import threading
from functools import lru_cache

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_pending = {}
_running = False


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
    if not enabled():
        return
    global _running
    with _lock:
        _pending[library, scope] = job_id
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
            (library, scope), job_id = _pending.popitem()
        try:
            _client().send_to_group(group(library), {
                'type': 'index-ready', 'libraryId': library, 'scope': scope, 'jobId': job_id,
            }, content_type='application/json')
        except Exception:
            _log.warning('Index-ready push failed; clients can recover through job polling', exc_info=True)
