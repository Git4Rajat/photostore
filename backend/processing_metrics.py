"""Bounded in-memory counters; no identifiers, payloads or storage operations.

Each HTTP operation contributes once to each enclosing image/wave counter.
Context propagation shares the wave counter while concurrent images own distinct
counters. Durations of concurrent work are totals, never wall-time deductions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

_COUNTERS = ContextVar('processing_io_counters', default=())
_PURPOSE = ContextVar('processing_io_purpose', default='other')
_PURPOSES = {'inspection', 'qualification', 'claim', 'heartbeat', 'source_release',
             'artifact_write', 'derivative_promotion', 'artifact_capture',
             'face_persistence', 'semantic_refresh', 'embedding_write',
             'metadata_commit', 'metadata_cas', 'lease_and_policy_check', 'other'}
_FIELDS = {
    'storage_calls', 'storage_send_ms', 'storage_bytes', 'storage_errors',
    'metadata_point_reads', 'metadata_query_requests', 'metadata_read_ms',
    'job_read_requests', 'blob_gets', 'blob_heads',
    'batch_requests', 'batch_queries', 'batch_keys', 'batch_rows', 'batch_missing', 'batch_point_reads',
    'receipt_cache_hits', 'receipt_cache_misses', 'receipt_cache_evictions',
    'receipt_write_batches', 'vision_inspection_prefetches', 'vision_inspection_prefetch_ms', 'vision_inspection_prefetch_errors',
    'artifact_gets', 'artifact_heads', 'artifact_verified_bytes', 'artifact_verifications',
    'artifact_verification_reuse', 'artifact_create_conflicts', 'receipt_creates', 'receipt_create_conflicts',
    'journal_flushes', 'journal_flush_ms', 'journal_flush_errors',
    'journal_fsyncs', 'journal_fsync_ms', 'journal_fsync_errors',
    'image_passes', 'image_inspections', 'source_attempts', 'source_errors',
    'vision_source_queue_ms', 'vision_source_busy_ms', 'vision_pool_ms', 'vision_pool_capacity_ms',
    'vision_coordinator_wait_ms', 'vision_admission_ms', 'vision_admission_batches', 'vision_settlement_ms',
    'ocr_engine_init_ms', 'ocr_engine_inits', 'ocr_recognition_ms',
    'ocr_worker_starts', 'ocr_worker_reuses', 'ocr_worker_timeouts', 'ocr_worker_capacity_errors',
    'clip_preprocess_ms', 'clip_lock_wait_ms', 'clip_inference_ms',
    'face_decode_ms', 'face_detect_ms', 'face_landmark_wait_ms', 'face_landmark_ms',
    'face_align_ms', 'face_embed_ms',
    'image_inspection_ms', 'image_processing_ms', 'image_qualification_ms',
} | {'metadata_' + purpose + '_reads' for purpose in _PURPOSES} | {
    'vision_source_' + phase + suffix
    for phase in ('claim', 'steps', 'apply', 'qualify', 'release')
    for suffix in ('_calls', '_ms', '_errors')
}


class Counters:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def add(self, values):
        with self.lock:
            for key, value in values.items():
                if key in _FIELDS:
                    self.values[key] = self.values.get(key, 0) + value

    def snapshot(self):
        with self.lock:
            return {key: self.values.get(key, 0) for key in sorted(_FIELDS)}


@contextmanager
def accounting(counter=None):
    counter = counter if counter is not None else Counters()
    parents = _COUNTERS.get()
    token = _COUNTERS.set(parents if counter in parents else (*parents, counter))
    try:
        yield counter
    finally:
        _COUNTERS.reset(token)


@contextmanager
def purpose(name):
    token = _PURPOSE.set(name if name in _PURPOSES else 'other')
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def add(**values):
    for counter in _COUNTERS.get():
        counter.add(values)


def record_io(kind, method, path, status, milliseconds, size):
    if not _COUNTERS.get(): return
    values = dict(storage_calls=1, storage_send_ms=milliseconds, storage_bytes=size,
                  storage_errors=int(status <= 0 or status >= 400))
    resource = path.split('?', 1)[0].strip('/').split('/', 1)[0]
    if kind == 'table' and method == 'GET':
        table = resource.split('(', 1)[0].lower()
        if table == 'photometadata':
            values.update(metadata_read_ms=milliseconds)
            values['metadata_point_reads' if '(partitionkey=' in resource.lower() else 'metadata_query_requests'] = 1
            values['metadata_' + _PURPOSE.get() + '_reads'] = 1
        elif table == 'photojobs':
            values['job_read_requests'] = 1
    if kind == 'blob' and method in ('GET', 'HEAD'):
        values['blob_gets' if method == 'GET' else 'blob_heads'] = 1
    add(**values)


def inherit(function, *, purpose_name='heartbeat'):
    """Carry only accounting into a raw thread, without copying execution fences."""
    counters = _COUNTERS.get()
    def run():
        token = _COUNTERS.set(counters)
        try:
            with purpose(purpose_name):
                return function()
        finally:
            _COUNTERS.reset(token)
    return run


@contextmanager
def timer(name, *, count_name=None):
    started = time.monotonic()
    try:
        yield
    except BaseException:
        add(**{name + '_errors': 1})
        raise
    finally:
        plural = count_name or name + ('es' if name.endswith('flush') else 's')
        add(**{plural: 1, name + '_ms': (time.monotonic() - started) * 1000})
