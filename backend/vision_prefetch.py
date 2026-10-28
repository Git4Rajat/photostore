"""Bounded preview lookahead; claimed source identity is rechecked on take."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import os
from pathlib import Path
import tempfile
import threading
import time

import processing_metrics as metrics

_CURRENT = ContextVar('vision_preview_prefetch', default=None)
MAX_PREVIEW_BYTES = 2 * 1024 * 1024


class PrefetchUnavailable(RuntimeError):
    pass


def binding(library, filename, row):
    return (library, filename, row.get('anonymousImageId'), row.get('processingRevision'), row.get('fileHash'))


def prefetched_preview(library, filename, row):
    current = _CURRENT.get()
    return current.take(library, filename, row) if current is not None else None


class PreviewPrefetch:
    def __init__(self, loader, *, workers=None, byte_limit=None, storage=None):
        from vision_buffer import limit_bytes
        workers = int(os.getenv('IPWORK_VISION_PREFETCH_WORKERS', '4')) if workers is None else workers
        byte_limit = limit_bytes('IPWORK_VISION_PREFETCH_BUFFER_MB', 16, 64) if byte_limit is None else byte_limit
        if not 1 <= workers <= 4 or byte_limit < MAX_PREVIEW_BYTES:
            raise ValueError('Invalid preview buffer limits')
        self.loader, self.workers, self.limit = loader, workers, byte_limit
        self.storage = storage or os.getenv('IPWORK_VISION_PREFETCH_STORAGE', 'memory')
        if self.storage not in ('memory', 'disk'):
            raise ValueError('Invalid preview prefetch storage')
        self.pending, self.candidates = {}, deque()
        self.pool = self.directory = None
        self.reserved = 0
        self.lock = threading.RLock()

    def __enter__(self):
        if self.loader is not None:
            self.pool = ThreadPoolExecutor(self.workers, thread_name_prefix='vision-preview')
            if self.storage == 'disk':
                self.directory = tempfile.TemporaryDirectory(prefix='vision-previews-')
        self.token = _CURRENT.set(self)
        return self

    def _download(self, row, index):
        started = time.monotonic()
        try:
            data = self.loader(row, MAX_PREVIEW_BYTES)
            if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PREVIEW_BYTES:
                raise ValueError('Prefetched preview exceeds buffer limit')
            metrics.add(preview_prefetch_bytes=len(data))
            if self.directory is not None:
                path = Path(self.directory.name) / str(index)
                path.write_bytes(data)
                metrics.add(preview_prefetch_disk_bytes=len(data))
                return path
            return data
        except Exception:
            metrics.add(preview_prefetch_errors=1)
            return None
        finally:
            metrics.add(preview_prefetch_download_ms=(time.monotonic() - started) * 1000)

    def _fill(self):
        with self.lock:
            self._fill_locked()

    def _fill_locked(self):
        while self.candidates and len(self.pending) < self.workers and self.reserved + MAX_PREVIEW_BYTES <= self.limit:
            key, row = self.candidates.popleft()
            index = self.next_index
            self.next_index += 1
            self.reserved += MAX_PREVIEW_BYTES
            self.pending[key] = self.pool.submit(metrics.inherit(
                lambda row=row, index=index: self._download(row, index), purpose_name='prefetch'))
            metrics.add(preview_prefetch_submitted=1)
            metrics.observe(preview_prefetch_reserved_peak_bytes=self.reserved,
                            preview_prefetch_slots_peak=len(self.pending))

    def prepare(self, members, metadata):
        if self.pool is None:
            return
        self._drain()
        metrics.add(preview_prefetch_skipped_candidates=len(self.candidates))
        self.candidates.clear()
        self.next_index = 0
        from processing_completion import TERMINAL
        for member in members:
            from azure.core.exceptions import ResourceNotFoundError
            try:
                row = metadata.get_entity(metadata.library, member['filename'])
            except ResourceNotFoundError:
                continue
            if (row.get('anonymousImageId') != member['photoId'] or
                    row.get('processingRevision') != member['processingRevision'] or
                    row.get('processing_state') == 'deleted' or row.get('processingIsolated') or
                    row.get('preview_status') != 'done' or
                    not any(row.get(step + '_status') not in TERMINAL
                            for step in ('ocr', 'face', 'ai_vision'))):
                continue
            from image_utils import is_video_file
            if not is_video_file(member['filename']):
                self.candidates.append((binding(metadata.library, member['filename'], row), dict(row)))
        self._fill()

    def take(self, library, filename, row):
        key = binding(library, filename, row)
        with self.lock:
            future = self.pending.pop(key, None)
        if future is None:
            if self.pool is not None:
                metrics.add(preview_prefetch_misses=1)
            return None
        started = time.monotonic()
        try:
            value = future.result()
            if isinstance(value, Path):
                data = value.read_bytes()
                value.unlink()
            else:
                data = value
            if data is None:
                metrics.add(preview_prefetch_misses=1)
                raise PrefetchUnavailable('Prefetched preview unavailable')
            metrics.add(preview_prefetch_hits=1)
            return data
        except (OSError, ValueError):
            metrics.add(preview_prefetch_errors=1, preview_prefetch_misses=1)
            raise PrefetchUnavailable('Buffered preview unavailable')
        finally:
            with self.lock:
                self.reserved -= MAX_PREVIEW_BYTES
                self._fill_locked()
            metrics.add(preview_prefetch_wait_ms=(time.monotonic() - started) * 1000)

    def _drain(self):
        started = time.monotonic()
        for future in self.pending.values():
            value = future.result()
            if value is not None:
                size = value.stat().st_size if isinstance(value, Path) else len(value)
                metrics.add(preview_prefetch_unused=1, preview_prefetch_unused_bytes=size)
            if isinstance(value, Path):
                value.unlink(missing_ok=True)
        self.pending.clear()
        self.reserved = 0
        metrics.add(preview_prefetch_drain_ms=(time.monotonic() - started) * 1000)

    def __exit__(self, *args):
        try:
            self._drain()
        finally:
            if self.pool is not None:
                self.pool.shutdown(wait=True)
            metrics.add(preview_prefetch_skipped_candidates=len(self.candidates))
            self.candidates.clear()
            self.pending.clear()
            self.pool = None
            if self.directory is not None:
                self.directory.cleanup()
            _CURRENT.reset(self.token)
