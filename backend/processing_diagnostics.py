"""Bounded, best-effort processing telemetry. No photo data or storage calls."""
import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from ipworker_metrics import replica_identity, resource_sample
import processing_metrics as io_metrics

_LOGGER = logging.getLogger('processing_diagnostics')
_INSTANCE = uuid.uuid4().hex
_WORKERS = {}
_LABEL = re.compile(r'^[a-zA-Z0-9_.:-]{1,80}$')
_STATES = ('startup', 'idle', 'accumulating', 'processing', 'checkpoint')
_BATCH = ContextVar('processing_upload_batch', default=None)
_TRACE = ContextVar('processing_message', default=None)
_MEDIA = ContextVar('processing_media_download', default=None)
_IMAGES = ContextVar('processing_image_metrics', default=None)


def storage_tracing_enabled():
    import perf_instrumentation
    return bool(perf_instrumentation.ENABLED and perf_instrumentation._transport_patched)


@contextmanager
def stage_context(wave_id, stage, claim):
    fields = dict(_TRACE.get() or {}, wave_id=wave_id, stage=stage,
                  attempt_id=claim.owner, dispatch_attempt=claim.attempt,
                  schedule_id=claim.schedule_id or None)
    token = _TRACE.set(fields)
    try:
        yield
    finally:
        _TRACE.reset(token)


@contextmanager
def image_scope(member_key, phase):
    images = _IMAGES.get()
    if images is None:
        yield
        return
    counter = images[member_key]
    started = time.monotonic()
    with io_metrics.accounting(counter), io_metrics.purpose(phase):
        if phase == 'processing': io_metrics.add(source_attempts=1)
        try:
            yield
        except BaseException:
            if phase == 'processing': io_metrics.add(source_errors=1)
            raise
        finally:
            io_metrics.add(**{'image_' + phase + '_ms': (time.monotonic() - started) * 1000})


def vision_group_summary(function):
    @wraps(function)
    def wrapped(store, wave_id, members, *args, **kwargs):
        images = {member['RowKey']: io_metrics.Counters() for member in members}
        token = _IMAGES.set(images)
        observations, outcome = [], 'error'
        try:
            observations = function(store, wave_id, members, *args, **kwargs)
            outcome = 'success'
            return observations
        finally:
            _IMAGES.reset(token)
            qualified = {member['RowKey'] for member, completion, _ in observations if completion}
            for member in members:
                counter = images[member['RowKey']]
                io_metrics.add(image_passes=1)
                counter.add(dict(image_passes=1))
                file_event('vision_image_summary', store.library, member['filename'], wave_id=wave_id,
                    member_id=file_id(store.library, member['RowKey']),
                    measurement_scope='group_pass', outcome=outcome,
                    receipt_qualified=member['RowKey'] in qualified, storage_attribution='image_context',
                    storage_tracing_enabled=storage_tracing_enabled(),
                    **counter.snapshot())
    return wrapped


def vision_wave_summary(function):
    @wraps(function)
    def wrapped(leases, work, store, library, wave_id, *args, **kwargs):
        started, cpu = time.monotonic(), time.process_time()
        result, outcome = None, 'error'
        with io_metrics.accounting() as counter:
            try:
                result = function(leases, work, store, library, wave_id, *args, **kwargs)
                outcome = 'success' if result is not None else 'no_work'
                return result
            finally:
                wave = result or {}
                trace_fields = {key: value for key, value in (_TRACE.get() or {}).items()
                                if key in ('message_id', 'batch_id', 'attempt_id', 'dispatch_attempt', 'schedule_id')}
                emit('vision_wave_attempt_summary', wave_id=wave_id, library_id=file_id(library, ''),
                    measurement_scope='attempt', outcome=outcome, state=wave.get('state'),
                    vision_complete=wave.get('state') in ('ready', 'indexing', 'complete'),
                    member_count=wave.get('memberCount'), failed_members=wave.get('failedMembers'),
                    milliseconds=(time.monotonic() - started) * 1000,
                    process_cpu_ms=(time.process_time() - cpu) * 1000, cpu_attribution='process',
                    storage_tracing_enabled=storage_tracing_enabled(),
                    **trace_fields,
                    **counter.snapshot())
    return wrapped


def media_download_context():
    return dict(_MEDIA.get() or {})


@contextmanager
def media_download_scope(purpose, *, user=None, filename=None, context=None):
    fields = dict(context if context is not None else _MEDIA.get() or {})
    if user is not None and filename is not None:
        fields.update(_TRACE.get() or {})
        fields.update(file_id=file_id(user, filename), library_id=file_id(user, ''))
    fields['purpose'] = purpose if purpose in {
        'inference', 'exif_fallback', 'face_cover', 'result_fallback', 'metadata_packet',
    } else 'unattributed'
    token = _MEDIA.set(fields)
    try:
        yield
    finally:
        _MEDIA.reset(token)


def media_download(kind, size, milliseconds, outcome='success'):
    try:
        fields = media_download_context()
        fields.setdefault('purpose', 'unattributed')
        emit('media_download', **fields, role=os.getenv('APP_ROLE', 'unknown'),
             source=kind if kind in {'image', 'thumbnail', 'cover', 'metadata_packet'} else 'other',
             bytes=size, milliseconds=milliseconds, outcome=outcome)
    except Exception:
        pass


def batch_id(value):
    # Accept only UUIDs, never arbitrary caller strings or filenames.
    try:
        return uuid.UUID(value).hex if isinstance(value, str) and len(value) in (32, 36) else None
    except (ValueError, TypeError, AttributeError):
        return None


def file_id(user, filename):
    return hashlib.sha256(json.dumps([str(user), str(filename)], separators=(',', ':')).encode()).hexdigest()[:32]


def emit(event, **fields):
    try:
        record = {'event': event, 'schema': 1, 'process_instance': _INSTANCE,
                  'process_id': os.getpid(), 'identity': replica_identity(),
                  'at': datetime.now(timezone.utc).isoformat()}
        for key, value in fields.items():
            if isinstance(value, bool) or value is None:
                record[key] = value
            elif isinstance(value, (int, float)) and math.isfinite(value):
                record[key] = round(value, 6)
            elif isinstance(value, str) and _LABEL.fullmatch(value):
                record[key] = value
        _LOGGER.info('processing telemetry %s', json.dumps(record, separators=(',', ':'), sort_keys=True))
    except Exception:
        pass


def file_event(event, user, filename, **fields):
    try:
        fields = {**(_TRACE.get() or {}), **{key: value for key, value in fields.items() if value is not None}}
        emit(event, file_id=file_id(user, filename), library_id=file_id(user, ''), **fields)
    except Exception:
        pass


@contextmanager
def apply_phase(user, filename, phase):
    """Attribute apply work without retaining metadata or changing worker state."""
    def io_snapshot():
        try:
            from perf_instrumentation import current_scope
            scope = current_scope()
            if scope is not None:
                with scope.lock:
                    return scope, scope.io_count, scope.io_ms
        except Exception:
            pass
        return None

    started, cpu_started = time.monotonic(), time.thread_time()
    before = io_snapshot()
    outcome = 'success'
    try:
        with io_metrics.purpose(phase):
            yield
    except BaseException:
        outcome = 'error'
        raise
    finally:
        after = io_snapshot()
        same_scope = before is not None and after is not None and before[0] is after[0]
        file_event('processing_apply_phase', user, filename, phase=phase, outcome=outcome,
            milliseconds=max(0, time.monotonic() - started) * 1000,
            thread_cpu_ms=max(0, time.thread_time() - cpu_started) * 1000,
            sdk_calls=after[1] - before[1] if same_scope else None,
            sdk_send_ms=after[2] - before[2] if same_scope else None,
            sdk_attribution='shared_scope' if same_scope else None)


# One bounded accumulator per wave attempt; no per-face logging or retained IDs.
_WAVE = ContextVar('processing_wave_metrics', default=None)


def _scope_io():
    try:
        from perf_instrumentation import current_scope
        scope = current_scope()
        if scope is not None:
            with scope.lock:
                return scope, scope.io_count, scope.io_ms
    except Exception:
        pass
    return None


def _io_delta(before):
    after = _scope_io()
    if before is not None and after is not None and before[0] is after[0]:
        return dict(sdk_calls=after[1] - before[1], sdk_send_ms=after[2] - before[2],
                    sdk_attribution='shared_scope')
    return {}


class WaveMetrics:
    def __init__(self, library, wave_id):
        self.fields = dict(_TRACE.get() or {})
        self.fields.update(role='cluster', library_id=file_id(library, ''), wave_id=wave_id)
        self.phases = {}
        self.faces = 0
        self.faces_processed = 0

    @contextmanager
    def phase(self, name):
        started, cpu, before = time.monotonic(), time.thread_time(), _scope_io()
        outcome = 'success'
        counters = io_metrics.Counters()
        try:
            with io_metrics.accounting(counters):
                yield
        except BaseException:
            outcome = 'error'
            raise
        finally:
            row = self.phases.setdefault(name, dict(calls=0, errors=0, milliseconds=0.,
                                                    thread_cpu_ms=0.))
            row['calls'] += 1
            row['errors'] += outcome == 'error'
            row['milliseconds'] += max(0, time.monotonic() - started) * 1000
            row['thread_cpu_ms'] += max(0, time.thread_time() - cpu) * 1000
            for key, value in _io_delta(before).items():
                if key != 'sdk_attribution':
                    row[key] = row.get(key, 0) + value
            for key, value in counters.snapshot().items():
                if value:
                    row[key] = row.get(key, 0) + value


@contextmanager
def wave_phase_scope(name):
    metrics = _WAVE.get()
    if metrics is None:
        yield
    else:
        with metrics.phase(name):
            yield


def wave_faces(total=None, processed=None):
    metrics = _WAVE.get()
    if metrics is not None:
        if total is not None:
            metrics.faces = total
        if processed is not None:
            metrics.faces_processed = processed


def wave_phase(name):
    """Aggregate inclusive phase costs; nested phases must not be summed."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with wave_phase_scope(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def cluster_wave_summary(function):
    @wraps(function)
    def wrapped(leases, store, library, wave_id, *args, **kwargs):
        metrics = WaveMetrics(library, wave_id)
        token = _WAVE.set(metrics)
        started, cpu, before = time.monotonic(), time.process_time(), _scope_io()
        outcome = 'error'
        with io_metrics.accounting() as counters:
            try:
                result = function(leases, store, library, wave_id, *args, **kwargs)
                outcome = 'success' if result is not None else 'no_work'
                return result
            finally:
                _WAVE.reset(token)
                for name, totals in metrics.phases.items():
                    emit('cluster_wave_phase', **metrics.fields, phase=name,
                         inclusive=True, sdk_attribution='shared_scope', **totals)
                emit('cluster_wave_summary', **metrics.fields, outcome=outcome,
                     milliseconds=max(0, time.monotonic() - started) * 1000,
                     process_cpu_ms=max(0, time.process_time() - cpu) * 1000,
                     cpu_attribution='process', faces=metrics.faces,
                     faces_processed=metrics.faces_processed, measurement_scope='attempt',
                     state=result.get('state') if outcome == 'success' and isinstance(result, dict) else None,
                     storage_tracing_enabled=storage_tracing_enabled(),
                     **counters.snapshot(), **_io_delta(before))
    return wrapped


def payload(message):
    try:
        result = json.loads(message.content or '{}')
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def message_fields(message):
    body = payload(message)
    identifier = str(getattr(message, 'id', '') or body.get('jobId') or body.get('correlationId') or '')
    return {'message_id': hashlib.sha256(identifier.encode()).hexdigest()[:32] if identifier else None,
            'attempt': getattr(message, 'dequeue_count', None)}


@contextmanager
def trace(message):
    fields = message_fields(message)
    fields['batch_id'] = batch_id(payload(message).get('uploadBatchId'))
    fields['wave_id'] = batch_id(payload(message).get('waveId'))
    token = _TRACE.set(fields)
    try:
        yield
    finally:
        _TRACE.reset(token)


def message_event(event, message, *, role, **fields):
    try:
        body = payload(message)
        user = body.get('user_id') or body.get('userId') or ''
        names = body.get('filenames') or [body.get('filename')]
        inserted = getattr(message, 'insertion_time', None)
        age = max(0, (datetime.now(timezone.utc) - inserted).total_seconds()) if inserted else None
        for name in names[:1500] if isinstance(names, list) else []:
            if isinstance(name, str) and name:
                file_event(event, user, name, role=role,
                    batch_id=batch_id(body.get('uploadBatchId')),
                    enqueue_age_seconds=age, **message_fields(message), **fields)
    except Exception:
        pass


class Worker:
    """Exclusive resident-state accounting; CPU time is separate from wall time."""
    def __init__(self, role, *, clock=time.monotonic):
        self.role, self.clock = role, clock
        self.started = self.changed = self.last_report = clock()
        self.current = 'startup'
        self.seconds = dict.fromkeys(_STATES, 0.0)
        self.counts = {}
        self.last_cpu = resource_sample()['process_cpu_seconds']
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.first_work = False
        self.active = 0
        self.thread = None

    def state(self, value):
        with self.lock:
            now = self.clock()
            self.seconds[self.current] += max(0, now - self.changed)
            self.changed, self.current = now, value

    def count(self, name, value=1):
        if name not in {'applied_file_attempts', 'applied_face_attempts', 'acknowledged_messages', 'errors', 'received_messages', 'empty_receives'}:
            return
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + value

    def work_started(self):
        with self.lock:
            if not self.first_work:
                emit('worker_first_work', role=self.role, since_worker_start_seconds=max(0, self.clock() - self.started))
                self.first_work = True

    def report(self):
        try:
            with self.lock:
                self.state(self.current)
                now = self.clock()
                elapsed = max(0, now - self.started)
                resources = resource_sample()
                emit('worker_state', role=self.role, state=self.current, elapsed_seconds=elapsed,
                    window_seconds=max(0, now - self.last_report),
                    window_cpu_seconds=max(0, resources['process_cpu_seconds'] - self.last_cpu),
                    rss_bytes=resources['current_rss_bytes'], peak_rss_bytes=resources['peak_rss_bytes'],
                    **{key + '_seconds': value for key, value in self.seconds.items()}, **self.counts)
                self.last_report, self.last_cpu = now, resources['process_cpu_seconds']
        except Exception:
            pass

    def start(self):
        def run():
            while not self.stopping.wait(60):
                self.report()
        self.thread = threading.Thread(target=run, name='processing-diagnostics', daemon=True)
        self.thread.start()

    def stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.report()


def count(role, name, value=1):
    try:
        if role in _WORKERS:
            _WORKERS[role].count(name, value)
    except Exception:
        pass


@contextmanager
def phase(role, name, *, state=None):
    started, reporter, previous = time.monotonic(), _WORKERS.get(role), None
    if reporter is not None and state:
        with reporter.lock:
            previous = reporter.current
            if state == 'processing':
                reporter.active += 1
                reporter.work_started()
            reporter.state(state)
    outcome = 'success'
    try:
        yield
    except BaseException:
        outcome = 'error'
        raise
    finally:
        emit('worker_phase', role=role, phase=name, outcome=outcome,
             seconds=max(0, time.monotonic() - started))
        if reporter is not None and previous:
            with reporter.lock:
                if state == 'processing':
                    reporter.active -= 1
                    reporter.state('processing' if reporter.active else 'idle')
                else:
                    reporter.state(previous)


def measured(role, name, *, state=None):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with phase(role, name, state=state):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def call(role, name, function):
    with phase(role, name):
        return function()


def worker(role):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(),
                                format='%(asctime)s %(levelname)s %(name)s %(message)s')
            try:
                reporter = Worker(role)
                reporter.start()
            except Exception:
                emit('worker_diagnostics_unavailable', role=role)
                return function(*args, **kwargs)
            _WORKERS[role] = reporter
            atexit.register(reporter.stop)
            emit('worker_start', role=role)
            try:
                return function(*args, **kwargs)
            finally:
                reporter.stop()
                atexit.unregister(reporter.stop)
                _WORKERS.pop(role, None)
                emit('worker_stop', role=role)
        return wrapped
    return decorate


def ready(role, **fields):
    reporter = _WORKERS.get(role)
    if reporter is not None:
        emit('worker_ready', role=role, startup_seconds=max(0, reporter.clock() - reporter.started), **fields)
        reporter.state('idle')


def upload_fields(data):
    value = batch_id(data.get('uploadBatchId'))
    return {'processingBatchId': value} if value else {}


def upload_event(user, filename, data, *, size, duplicate, video):
    file_event('upload_finalized', user, filename, batch_id=batch_id(data.get('uploadBatchId')),
               bytes=size, duplicate=bool(duplicate), media_kind='video' if video else 'photo')


def queued_call(function, user, filename, data):
    """Context is only needed for the synchronous queue send; never retained."""
    # The module-level context below avoids per-library maps and extra reads.
    token = _BATCH.set(batch_id(data.get('uploadBatchId')))
    try:
        return function(user, filename)
    finally:
        _BATCH.reset(token)


def current_batch():
    return _BATCH.get()


def cluster_applied(user, prepared, message=None):
    try:
        for filename, ids in prepared:
            file_event('cluster_applied', user, filename, role='cluster', faces=len(set(ids)),
                       **message_fields(message))
        count('cluster', 'applied_file_attempts', len(prepared))
        count('cluster', 'applied_face_attempts', sum(len(set(ids)) for _, ids in prepared))
    except Exception:
        pass


def activity(user, entries, is_video, *, phase):
    try:
        names = [entry.get('filename') for entry in entries if isinstance(entry, dict)
                 and isinstance(entry.get('filename'), str)]
        videos = sum(bool(is_video(name)) for name in names)
        emit('upload_activity', library_id=file_id(user, ''), phase=phase,
             photos=len(names) - videos, videos=videos)
    except Exception:
        pass


def browser_batch(user, data):
    identifier = batch_id(data.get('batchId'))
    if not identifier or data.get('phase') not in ('start', 'end'):
        return False
    fields = {key: value for key in ('selected', 'photos', 'videos', 'uploaded', 'failed', 'duplicates', 'elapsedMs')
              if type(value := data.get(key)) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12}
    emit('browser_batch', library_id=file_id(user, ''), batch_id=identifier,
         phase=data['phase'], **fields)
    return True
