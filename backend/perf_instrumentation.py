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

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional

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
        if MEMORY_WARN_MB and rss > MEMORY_WARN_MB:
            _LOGGER.warning('PERF event=mem_high rss_mb=%.0f limit_mb=%.0f', rss, MEMORY_WARN_MB)


def install(flask_app) -> None:
    """Register request hooks and start the memory sampler (idempotent)."""
    if not ENABLED or getattr(flask_app, '_perf_installed', False):
        return
    flask_app._perf_installed = True
    from flask import request

    @flask_app.before_request
    def _perf_start():
        global _inflight, _inflight_peak
        _local.start = time.perf_counter()
        _local.rss = rss_mb()
        _local.spans = []
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
            spans = getattr(_local, 'spans', None) or []
            top = sorted(spans, key=lambda s: s[1], reverse=True)[:4]
            level = logging.WARNING if elapsed_ms >= SLOW_REQUEST_MS else logging.INFO
            _LOGGER.log(
                level,
                'PERF event=request method=%s path=%s status=%s ms=%.1f rss_mb=%.0f rss_delta_mb=%.0f '
                'inflight=%s bytes=%s spans=%s',
                request.method, request.path, response.status_code, elapsed_ms, rss,
                rss - getattr(_local, 'rss', rss), getattr(_local, 'queue_depth', '?'),
                response.calculate_content_length() or '?',
                ','.join(f'{n}:{ms:.0f}ms' for n, ms in top) or '-',
            )
        return response

    @flask_app.teardown_request
    def _perf_teardown(_exc):
        global _inflight
        if getattr(_local, 'start', None) is not None:
            with _inflight_lock:
                _inflight = max(0, _inflight - 1)
            _local.start = None
            _local.spans = None

    if MEMORY_SAMPLE_SECONDS > 0:
        threading.Thread(target=_sampler, name='perf-mem-sampler', daemon=True).start()
