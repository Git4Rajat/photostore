"""Lightweight request/span/memory instrumentation for the backend app.

Goal: find out what actually takes time and memory with ~130k photos. Everything
here logs one structured line per event (``PERF key=value ...``) so it can be
grepped/queried in Log Analytics without any extra dependency.

- ``install(app)``: per-request timing (wall, in-flight count, RSS delta, spans
  recorded during the request) and a periodic memory sampler thread.
- ``span(name, **fields)``: context manager to time a section (blob download,
  gunzip, json.loads, table scan ...). Adds itself to the current request's
  span summary when called inside a request.

Controlled by env: PERF_INSTRUMENTATION (default on), PERF_SLOW_REQUEST_MS
(default 1000), PERF_MEMORY_SAMPLE_SECONDS (default 15, 0 disables sampler),
PERF_MEMORY_WARN_MB (default 0 = off; log WARNING above this RSS).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import os
import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

_LOGGER = logging.getLogger('perf')

ENABLED = os.getenv('PERF_INSTRUMENTATION', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
SLOW_REQUEST_MS = float(os.getenv('PERF_SLOW_REQUEST_MS', '1000'))
MEMORY_SAMPLE_SECONDS = float(os.getenv('PERF_MEMORY_SAMPLE_SECONDS', '15'))
MEMORY_WARN_MB = float(os.getenv('PERF_MEMORY_WARN_MB', '0'))

_local = threading.local()
_inflight_lock = threading.Lock()
_inflight = 0
_inflight_peak = 0


def rss_mb() -> float:
    """Current resident set size in MB (Linux /proc; 0.0 if unavailable)."""
    try:
        with open('/proc/self/statm') as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE') / (1024 * 1024)
    except Exception:
        return 0.0


def _fmt(fields: Dict[str, object]) -> str:
    return ' '.join(f'{k}={v}' for k, v in fields.items() if v is not None)


def log_event(event: str, **fields) -> None:
    if ENABLED:
        _LOGGER.info('PERF event=%s %s', event, _fmt(fields))


# --- scopes: per-request / per-job accounting of storage round trips ------------------
# Every Azure SDK call (Table, Blob, Queue) goes through one transport; tracing
# it there counts round trips, bytes and duplicates for the request or job that
# caused them, without touching each call site. A scope lives in a contextvar
# (propagated into ThreadPoolExecutor workers, see _propagate_context_into_pools).
_scope_var: contextvars.ContextVar = contextvars.ContextVar('perf_scope', default=None)
IO_DUP_WARN = int(os.getenv('PERF_IO_DUP_WARN', '3'))      # same call repeated this often => event=dup_io
IO_TRACE_TOP = int(os.getenv('PERF_IO_TOP', '6'))
_UUIDISH = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_totals_lock = threading.Lock()
_io_totals: Dict[str, List[float]] = {}   # op -> [count, ms, bytes]


def _io_kind(host: str) -> str:
    host = (host or '').lower()
    if '.table.' in host or host.endswith('.table.core.windows.net'):
        return 'table'
    if '.blob.' in host:
        return 'blob'
    if '.queue.' in host:
        return 'queue'
    if '.file.' in host:
        return 'file'
    return host.split('.')[0] or 'http'


def _op_label(kind: str, method: str, path: str) -> str:
    """Stable, low-cardinality label: container/table + verb, ids collapsed."""
    parts = [p for p in path.split('?', 1)[0].split('/') if p]
    head = parts[0] if parts else ''
    head = re.sub(r"\(.*\)$", '(..)', head)          # Tables(PartitionKey='x',RowKey='y')
    head = _UUIDISH.sub('{id}', head)
    return f'{kind}:{method}:{head or "/"}'


class Scope:
    """Accumulates timing + I/O for one request or background job."""

    def __init__(self, name: str, **fields) -> None:
        self.name = name
        self.fields = fields
        self.start = time.perf_counter()
        self.rss_start = rss_mb()
        self.rss_peak = self.rss_start
        self.lock = threading.Lock()
        self.ops: Dict[str, List[float]] = {}          # label -> [count, ms, bytes, errors]
        self.calls: Dict[Tuple[str, str], int] = {}    # (method, full path) -> count
        self.spans: List[Tuple[str, float]] = []
        self.io_count = 0
        self.io_ms = 0.0
        self.io_bytes = 0

    def add_io(self, kind: str, method: str, path: str, status: int, ms: float, nbytes: int) -> None:
        label = _op_label(kind, method, path)
        with self.lock:
            row = self.ops.setdefault(label, [0, 0.0, 0, 0])
            row[0] += 1
            row[1] += ms
            row[2] += nbytes
            if status >= 400:
                row[3] += 1
            key = (method, f'{kind}:{path}')
            self.calls[key] = self.calls.get(key, 0) + 1
            self.io_count += 1
            self.io_ms += ms
            self.io_bytes += nbytes

    def note_rss(self) -> None:
        self.rss_peak = max(self.rss_peak, rss_mb())

    def duplicates(self) -> List[Tuple[str, str, int]]:
        with self.lock:
            return sorted(
                ((m, p, n) for (m, p), n in self.calls.items() if n >= IO_DUP_WARN),
                key=lambda t: t[2], reverse=True,
            )

    def top_ops(self, n: int = IO_TRACE_TOP) -> str:
        with self.lock:
            ranked = sorted(self.ops.items(), key=lambda kv: kv[1][1], reverse=True)[:n]
        return ','.join(
            f'{label}:x{int(r[0])}/{r[1]:.0f}ms/{r[2] / 1024:.0f}KB' + (f'/err{int(r[3])}' if r[3] else '')
            for label, r in ranked
        ) or '-'


def current_scope() -> Optional[Scope]:
    return _scope_var.get()


def record_io(kind: str, method: str, path: str, status: int, ms: float, nbytes: int) -> None:
    op = _op_label(kind, method, path)
    with _totals_lock:
        row = _io_totals.setdefault(op, [0, 0.0, 0])
        row[0] += 1
        row[1] += ms
        row[2] += nbytes
    scope_obj = _scope_var.get()
    if scope_obj is not None:
        scope_obj.add_io(kind, method, path, status, ms, nbytes)


def _flush_io_totals() -> None:
    with _totals_lock:
        snapshot = dict(_io_totals)
        _io_totals.clear()
    if not snapshot:
        return
    ranked = sorted(snapshot.items(), key=lambda kv: kv[1][1], reverse=True)[:12]
    _LOGGER.info('PERF event=io_totals ops=%s', ','.join(
        f'{op}:x{int(r[0])}/{r[1]:.0f}ms/{r[2] / 1024:.0f}KB' for op, r in ranked))


@contextmanager
def scope(name: str, **fields):
    """Account a background job (index build, clustering step...) the same way
    requests are accounted. Logs one `scope_summary` line with the wall time,
    storage round trips, bytes, peak RSS, the slowest spans and any call that
    was repeated (duplicate work)."""
    if not ENABLED:
        yield None
        return
    sc = Scope(name, **fields)
    token = _scope_var.set(sc)
    try:
        yield sc
    finally:
        _scope_var.reset(token)
        log_scope_summary(sc, 'scope_summary')


def log_scope_summary(sc: Scope, event: str, **extra) -> None:
    sc.note_rss()
    elapsed_ms = (time.perf_counter() - sc.start) * 1000
    top_spans = ','.join(f'{n}:{ms:.0f}ms' for n, ms in sorted(sc.spans, key=lambda s: s[1], reverse=True)[:6]) or '-'
    _LOGGER.info(
        'PERF event=%s name=%s ms=%.0f io_calls=%d io_ms=%.0f io_mb=%.1f rss_peak_mb=%.0f rss_delta_mb=%.0f spans=%s io_top=%s %s',
        event, sc.name, elapsed_ms, sc.io_count, sc.io_ms, sc.io_bytes / 1048576, sc.rss_peak,
        sc.rss_peak - sc.rss_start, top_spans, sc.top_ops(), _fmt({**sc.fields, **extra}),
    )
    for method, path, n in sc.duplicates()[:5]:
        _LOGGER.warning('PERF event=dup_io scope=%s method=%s call=%s times=%d', sc.name, method, path, n)


@contextmanager
def step(name: str, **fields):
    """A named phase of a job: a span that also records the storage calls made
    inside it (`io_calls`, `io_ms`, `io_mb`) so a slow step can be told apart
    from a chatty one."""
    sc = _scope_var.get()
    before = (sc.io_count, sc.io_ms, sc.io_bytes) if sc else (0, 0.0, 0)
    with span(name, **fields):
        yield
    if sc:
        _LOGGER.info(
            'PERF event=step name=%s io_calls=%d io_ms=%.0f io_mb=%.1f',
            name, sc.io_count - before[0], sc.io_ms - before[1], (sc.io_bytes - before[2]) / 1048576,
        )


_transport_patched = False


def install_storage_tracing() -> None:
    """Wrap the Azure SDK's requests transport so every Table/Blob/Queue round
    trip is timed and attributed to the current scope. Idempotent."""
    global _transport_patched
    if not ENABLED or _transport_patched:
        return
    try:
        from azure.core.pipeline.transport import RequestsTransport
    except Exception:
        return
    original = RequestsTransport.send

    def traced_send(self, request, **kwargs):
        start = time.perf_counter()
        status, nbytes = 0, 0
        try:
            response = original(self, request, **kwargs)
            try:
                status = int(response.status_code)
                nbytes = int(response.headers.get('Content-Length') or 0)
            except Exception:
                pass
            return response
        finally:
            try:
                from urllib.parse import urlsplit
                parts = urlsplit(request.url)
                # Table queries all share one path (/table()); the OData $filter/$select is what
                # distinguishes them. Fold a short hash of the query into the duplicate key so a
                # repeat means the *same* query, not just the same table.
                path = parts.path
                if parts.query and '$filter' in parts.query:
                    path = f'{path}?q={hashlib.sha1(parts.query.encode()).hexdigest()[:8]}'
                record_io(_io_kind(parts.hostname or ''), str(request.method or 'GET').upper(), path,
                          status, (time.perf_counter() - start) * 1000, nbytes)
            except Exception:
                pass

    RequestsTransport.send = traced_send
    _transport_patched = True


_pools_patched = False


def _propagate_context_into_pools() -> None:
    """ThreadPoolExecutor workers don't inherit contextvars; without this the
    storage calls made by fan-out helpers would not be attributed to their
    request/job scope."""
    global _pools_patched
    if _pools_patched:
        return
    original_submit = concurrent.futures.ThreadPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return original_submit(self, ctx.run, fn, *args, **kwargs)

    concurrent.futures.ThreadPoolExecutor.submit = submit
    _pools_patched = True


@contextmanager
def span(name: str, **fields):
    """Time a section. Logs on exit and records into the active request."""
    if not ENABLED:
        yield
        return
    start = time.perf_counter()
    rss_before = rss_mb()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        rss_after = rss_mb()
        spans = getattr(_local, 'spans', None)
        if spans is not None:
            spans.append((name, elapsed_ms))
        active = _scope_var.get()
        if active is not None:
            active.spans.append((name, elapsed_ms))
            active.rss_peak = max(active.rss_peak, rss_after)
        _LOGGER.info(
            'PERF event=span name=%s ms=%.1f rss_mb=%.0f rss_delta_mb=%.0f %s',
            name, elapsed_ms, rss_after, rss_after - rss_before, _fmt(fields),
        )


def _sampler() -> None:
    while True:
        time.sleep(MEMORY_SAMPLE_SECONDS)
        rss = rss_mb()
        with _inflight_lock:
            inflight, peak = _inflight, _inflight_peak
        _LOGGER.info(
            'PERF event=mem rss_mb=%.0f threads=%d inflight=%d inflight_peak=%d',
            rss, threading.active_count(), inflight, peak,
        )
        _flush_io_totals()
        if MEMORY_WARN_MB and rss > MEMORY_WARN_MB:
            _LOGGER.warning('PERF event=mem_high rss_mb=%.0f limit_mb=%.0f', rss, MEMORY_WARN_MB)


def install(flask_app) -> None:
    """Register request hooks and start the memory sampler (idempotent)."""
    if not ENABLED or getattr(flask_app, '_perf_installed', False):
        return
    flask_app._perf_installed = True
    from flask import request

    install_storage_tracing()
    _propagate_context_into_pools()

    @flask_app.before_request
    def _perf_start():
        global _inflight, _inflight_peak
        if request.method == 'OPTIONS':
            _local.start = None
            return
        _local.start = time.perf_counter()
        _local.rss = rss_mb()
        _local.spans = []
        # Correlation: the browser sends its own id (and which view/session made
        # the call) so a slow client-side request can be matched to this line.
        _local.rid = (request.headers.get('X-Request-ID') or uuid.uuid4().hex[:8])[:40]
        _local.view = (request.headers.get('X-Client-View') or '')[:40]
        _local.session = (request.headers.get('X-Client-Session') or '')[:16]
        _local.scope = Scope(f'{request.method} {request.path}')
        _local.scope_token = _scope_var.set(_local.scope)
        with _inflight_lock:
            _inflight += 1
            _inflight_peak = max(_inflight_peak, _inflight)
            _local.queue_depth = _inflight

    @flask_app.after_request
    def _perf_end(response):
        start: Optional[float] = getattr(_local, 'start', None)
        if start is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000
            rss = rss_mb()
            sc: Optional[Scope] = getattr(_local, 'scope', None)
            spans = getattr(_local, 'spans', None) or []
            top = sorted(spans, key=lambda s: s[1], reverse=True)[:4]
            level = logging.WARNING if elapsed_ms >= SLOW_REQUEST_MS else logging.INFO
            io_calls = sc.io_count if sc else 0
            io_ms = sc.io_ms if sc else 0.0
            _LOGGER.log(
                level,
                'PERF event=request method=%s path=%s status=%s ms=%.1f rss_mb=%.0f rss_delta_mb=%.0f '
                'inflight=%s bytes=%s rid=%s view=%s sess=%s io_calls=%d io_ms=%.0f io_mb=%.2f spans=%s io_top=%s',
                request.method, request.path, response.status_code, elapsed_ms, rss,
                rss - getattr(_local, 'rss', rss), getattr(_local, 'queue_depth', '?'),
                response.calculate_content_length() or '?',
                getattr(_local, 'rid', '-'), getattr(_local, 'view', '') or '-', getattr(_local, 'session', '') or '-',
                io_calls, io_ms, (sc.io_bytes if sc else 0) / 1048576,
                ','.join(f'{n}:{ms:.0f}ms' for n, ms in top) or '-',
                sc.top_ops() if sc else '-',
            )
            if sc is not None:
                for method, path, n in sc.duplicates()[:3]:
                    _LOGGER.warning(
                        'PERF event=dup_io scope=%s method=%s call=%s times=%d rid=%s',
                        sc.name, method, path, n, getattr(_local, 'rid', '-'),
                    )
            # Server-Timing lets DevTools and the client's perf module split
            # "server time" from network + queueing without any extra request.
            response.headers['X-Request-ID'] = getattr(_local, 'rid', '')
            response.headers['Server-Timing'] = (
                f'app;dur={elapsed_ms:.0f}, storage;dur={io_ms:.0f};desc="{io_calls} calls"'
            )
        return response

    @flask_app.teardown_request
    def _perf_teardown(_exc):
        global _inflight
        token = getattr(_local, 'scope_token', None)
        if token is not None:
            try:
                _scope_var.reset(token)
            except ValueError:
                pass
            _local.scope_token = None
        if getattr(_local, 'start', None) is not None:
            with _inflight_lock:
                _inflight = max(0, _inflight - 1)
            _local.start = None
            _local.spans = None
            _local.scope = None

    if MEMORY_SAMPLE_SECONDS > 0:
        threading.Thread(target=_sampler, name='perf-mem-sampler', daemon=True).start()
