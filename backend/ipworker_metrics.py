"""Dependency-free, bounded process-local ipworker diagnostics.

No observation samples, message IDs, filenames or tenant labels are retained.
Quantiles are histogram bucket upper bounds, not interpolated measurements.
"""
from bisect import bisect_left
import ctypes
from functools import lru_cache
import math
import os
import sys
import threading
import time

try:
    import resource
except ImportError:
    resource = None


LATENCY_MS = (1, 5, 10, 25, 50, 100, 250, 500, 750, 1000, 2000, 5000,
              10000, 30000, 60000, 120000, 300000, 600000)
RETRY_DEPTH = (0, 1, 2, 3, 5, 8, 16, 32, 64)
STEPS = ('preview', 'thumbnail', 'exif', 'ocr', 'face', 'ai_vision', 'map_detection')
COUNTERS = ('done', 'noop', 'lease_busy', 'error', 'not_found', 'retry_exhausted',
            'receive', 'receive_failed', 'received', 'ack', 'ack_failed',
            'productive_completed', 'completed_with_step_error', 'completed_result_unknown', 'already_processed',
            'eligibility_reprocessing', 'eligibility_unknown',
            'defer_shutdown', 'defer_preparation_failed', 'defer_visibility_budget',
            'watchdog_preparation', 'watchdog_tasks', 'shutdown_grace_exhausted',
            'face_no_detection', 'face_postprocessing_failed', 'face_quality_rejected', 'face_embedded')
TIMINGS = ('receive', 'receive_failed', 'ack', 'ack_failed', 'download', 'lease',
           'steps', 'apply', 'cluster', 'task', 'receipt_to_start', 'receipt_to_ack',
           'preparation', 'wave') + tuple('step_' + step for step in STEPS) + tuple(
               'face_' + phase for phase in ('decode', 'detect', 'landmarkWait', 'landmark', 'align', 'embed'))


class Histogram:
    def __init__(self, bounds=LATENCY_MS):
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)
        self.total = 0.0

    def observe(self, value):
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return
        self.counts[bisect_left(self.bounds, value)] += 1
        self.total += value

    def snapshot(self):
        count = sum(self.counts)
        result = {'count': count, 'sum': round(self.total, 3),
                  'bucket_upper_bounds': list(self.bounds) + [None],
                  'bucket_counts': list(self.counts)}
        for percentile in (50, 95, 99):
            upper = None
            if count:
                rank = math.ceil(count * percentile / 100)
                cumulative = 0
                for index, frequency in enumerate(self.counts):
                    cumulative += frequency
                    if cumulative >= rank:
                        upper = self.bounds[index] if index < len(self.bounds) else None
                        break
            result[f'p{percentile}_upper_bound'] = upper
        return result


class _DarwinTimeValue(ctypes.Structure):
    _fields_ = [('seconds', ctypes.c_int32), ('microseconds', ctypes.c_int32)]


class _DarwinTaskBasicInfo(ctypes.Structure):
    _fields_ = [('virtual_size', ctypes.c_uint64), ('resident_size', ctypes.c_uint64),
                ('resident_size_max', ctypes.c_uint64), ('user_time', _DarwinTimeValue),
                ('system_time', _DarwinTimeValue), ('policy', ctypes.c_int32),
                ('suspend_count', ctypes.c_int32)]


@lru_cache(maxsize=1)
def _darwin_resource_api():
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    library.task_info.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_uint32)]
    library.task_info.restype = ctypes.c_int32
    return library


def _darwin_current_rss_bytes():
    """MACH_TASK_BASIC_INFO resident_size is bytes; no subprocess or polling."""
    library = _darwin_resource_api()
    task = ctypes.c_uint32.in_dll(library, 'mach_task_self_').value
    info = _DarwinTaskBasicInfo()
    count = ctypes.c_uint32(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int32))
    if library.task_info(task, 20, ctypes.byref(info), ctypes.byref(count)) != 0:
        return None
    return int(info.resident_size)


def resource_sample():
    """Bytes on both Linux and Darwin; missing data is null, never a fake zero."""
    result = {'process_cpu_seconds': time.process_time(),
              'current_rss_bytes': None, 'peak_rss_bytes': None}
    try:
        if resource is not None:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            result['peak_rss_bytes'] = int(usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        if sys.platform.startswith('linux'):
            with open('/proc/self/statm', encoding='ascii') as handle:
                result['current_rss_bytes'] = int(handle.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        elif sys.platform == 'darwin':
            result['current_rss_bytes'] = _darwin_current_rss_bytes()
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return result


def replica_identity():
    # Deliberate allowlist: never dump environment, connection strings or tokens.
    return {key.lower(): os.environ.get(key, '')[:128] for key in
            ('CONTAINER_APP_NAME', 'CONTAINER_APP_REVISION', 'CONTAINER_APP_REPLICA_NAME', 'HOSTNAME')}


class Metrics:
    def __init__(self, concurrency, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.concurrency = max(1, concurrency)
        self.cumulative = {}
        self.window = {}
        self.histograms = [{name: Histogram(RETRY_DEPTH if name == 'retry_depth' else LATENCY_MS)
                            for name in TIMINGS + ('retry_depth',)} for _ in range(2)]
        self.integrals = [{key: 0.0 for key in ('slot_seconds', 'preparation_only_seconds',
                                               'idle_seconds', 'ready_seconds', 'preparing_seconds')}
                          for _ in range(2)]
        self.active = 0
        self.preparing = self.ready = 0
        self.last_state_at = clock()
        self.last_resources = resource_sample()
        self.started_cpu_seconds = self.last_resources['process_cpu_seconds']

    def record(self, key, duration_ms=None, count=1):
        if key not in COUNTERS:
            return
        with self.lock:
            for counters in (self.cumulative, self.window):
                counters[key] = counters.get(key, 0) + count
                if duration_ms is not None:
                    counters[key + '_ms'] = counters.get(key + '_ms', 0) + duration_ms
            if duration_ms is not None:
                self.observe(key, duration_ms)

    def observe(self, name, value):
        if name not in self.histograms[0]:
            return
        with self.lock:
            for histograms in self.histograms:
                histograms[name].observe(value)

    def _integrate(self):
        now = self.clock()
        elapsed = max(0, now - self.last_state_at)
        for values in self.integrals:
            values['slot_seconds'] += self.active * elapsed
            values['preparing_seconds'] += bool(self.preparing) * elapsed
            values['ready_seconds'] += bool(self.ready) * elapsed
            values['preparation_only_seconds'] += bool(self.preparing and not self.active) * elapsed
            values['idle_seconds'] += bool(not (self.active or self.preparing or self.ready)) * elapsed
        self.last_state_at = now

    def state(self, *, preparing=None, ready=None, active_delta=0):
        with self.lock:
            self._integrate()
            self.active += active_delta
            if preparing is not None:
                self.preparing = preparing
            if ready is not None:
                self.ready = ready

    def snapshot(self, window_seconds, elapsed_seconds):
        with self.lock:
            self._integrate()
            output = {'latency_estimate': 'bucket_upper_bound; null means empty or overflow',
                      'histogram_units': {'latency': 'milliseconds', 'retry_depth': 'dequeue_count'},
                      'histograms': {}, 'utilization': {}}
            for index, (name, seconds) in enumerate((('window', window_seconds), ('cumulative', elapsed_seconds))):
                output[name] = {key: self.window.get(key, 0) if index == 0 else self.cumulative.get(key, 0)
                                for key in COUNTERS}
                counters = self.window if index == 0 else self.cumulative
                output[name].update({key: round(value, 3) for key, value in counters.items() if key.endswith('_ms')})
                output['histograms'][name] = {key: hist.snapshot() for key, hist in self.histograms[index].items()
                                             if sum(hist.counts)}
                values = self.integrals[index]
                output['utilization'][name] = {key: round(value, 6) for key, value in values.items()}
                output['utilization'][name]['slot_utilization'] = (
                    round(values['slot_seconds'] / (self.concurrency * seconds), 6) if seconds > 0 else None)
            resources = resource_sample()
            delta = max(0, resources['process_cpu_seconds'] - self.last_resources['process_cpu_seconds'])
            resources['window_cpu_seconds'] = round(delta, 6)
            resources['window_cpu_cores'] = round(delta / window_seconds, 6) if window_seconds > 0 else None
            resources['cumulative_cpu_seconds'] = round(max(0, resources['process_cpu_seconds'] - self.started_cpu_seconds), 6)
            resources['cumulative_cpu_cores'] = (round(resources['cumulative_cpu_seconds'] / elapsed_seconds, 6)
                                                 if elapsed_seconds > 0 else None)
            output['resources'] = resources
            self.last_resources = resources
            output['loop'] = {'active_tasks': self.active, 'preparing': self.preparing, 'ready': self.ready}
            self.window.clear()
            self.histograms[0] = {key: Histogram(hist.bounds) for key, hist in self.histograms[0].items()}
            self.integrals[0] = {key: 0.0 for key in self.integrals[0]}
            return output