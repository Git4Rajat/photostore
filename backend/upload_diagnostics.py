"""Bounded, request-local upload timings; never inspect request/response payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import time
import uuid

from flask import after_this_request, current_app, g
from werkzeug.exceptions import HTTPException

_CURRENT = ContextVar('upload_diagnostics', default=None)
# Only code-owned names may enter the record (including nested storage phases).
_PHASES = frozenset({
    'initialization', 'cleanup', 'tracking', 'sas_creation', 'name_resolution',
    'blob_check', 'finalize_metadata', 'metadata_stamp', 'metadata_read',
    'client_processing', 'enqueues', 'dedup', 'persistence', 'source_read',
})
_COUNTERS = frozenset({'files', 'succeeded', 'failed', 'steps'})


class UploadTiming:
    def __init__(self, route, clock=None):
        self.clock = clock or time.monotonic
        self.started = self._now()
        self.route = route
        self.correlation_id = uuid.uuid4().hex
        self.phase_ms = {}
        self.phase_errors = {}
        self.counters = {}
        self.emitted = False
        self.exception = False

    def _now(self):
        try:
            return self.clock()
        except Exception:
            return 0.0

    @contextmanager
    def phase(self, name):
        started = self._now()
        try:
            yield
        except BaseException:
            try:
                if name in _PHASES:
                    self.phase_errors[name] = min(1000000, self.phase_errors.get(name, 0) + 1)
            except Exception:
                pass
            raise
        finally:
            if name in _PHASES:
                try:
                    elapsed = max(0, (self._now() - started) * 1000)
                    self.phase_ms[name] = self.phase_ms.get(name, 0) + elapsed
                except Exception:
                    pass

    def count(self, name, value):
        try:
            if name in _COUNTERS:
                self.counters[name] = max(0, min(1000000, int(value)))
        except Exception:
            pass

    def emit(self, logger, status, *, exception=False):
        if self.emitted:
            return
        self.emitted = True
        try:
            outcome = ('exception' if exception else 'http_error' if status >= 400
                       else 'partial' if self.counters.get('failed')
                       else 'degraded' if self.phase_errors else 'success')
            record = {
                'correlation_id': self.correlation_id, 'route': self.route,
                'status': status, 'outcome': outcome,
                'total_ms': round(max(0, (self._now() - self.started) * 1000), 3),
                'phase_ms': {k: round(v, 3) for k, v in self.phase_ms.items()},
                'phase_errors': self.phase_errors, 'counts': self.counters,
            }
            logger.info('upload timings %s', json.dumps(record, sort_keys=True))
        except Exception:
            pass


@contextmanager
def upload_phase(name):
    timing = _CURRENT.get()
    if timing is None:
        yield
    else:
        with timing.phase(name):
            yield


def timed_call(name, function, *args, **kwargs):
    with upload_phase(name):
        return function(*args, **kwargs)


def upload_count(name, value):
    timing = _CURRENT.get()
    if timing is not None:
        timing.count(name, value)


def upload_results(results):
    """Aggregate only counts, never serialize individual file results."""
    try:
        failed = sum('error' in item for item in results)
        upload_count('failed', failed)
        upload_count('succeeded', len(results) - failed)
    except Exception:
        pass


def upload_teardown(error):
    """Fallback for propagated exceptions, where Flask never builds a response."""
    try:
        timing = getattr(g, '_upload_timing', None)
        if timing is not None and error is not None:
            status = error.code if isinstance(error, HTTPException) else 500
            timing.emit(current_app.logger, status, exception=timing.exception)
    except Exception:
        pass


def instrument_upload(route):
    """Keep the original return value and exceptions; observe HTTP status later."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                timing = UploadTiming(route)
                logger = current_app.logger
                g._upload_timing = timing
                token = _CURRENT.set(timing)
            except Exception:
                return function(*args, **kwargs)

            def log_response(response):
                timing.emit(logger, response.status_code, exception=timing.exception)
                return response

            try:
                after_this_request(log_response)
            except Exception:
                _CURRENT.reset(token)
                return function(*args, **kwargs)

            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                # Let error handlers select the real status before logging.
                timing.exception = not isinstance(exc, HTTPException)
                raise
            finally:
                _CURRENT.reset(token)
        return wrapped
    return decorate