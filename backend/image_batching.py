"""Attempt-scoped CLIP tensor batches; source identity/writes stay on callers."""
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
import math
import queue
import threading
import time

import processing_metrics as metrics

_ACTIVE = ContextVar('clip_image_batcher', default=None)


class ImageBatcher:
    def __init__(self, encode, fallback, *, batch_size=2, window=.002, dimensions=512):
        if type(batch_size) is not int or not 1 <= batch_size <= 8 or not 0 <= window <= .05:
            raise ValueError('Invalid CLIP batch size/window')
        self.encode, self.fallback = encode, fallback
        self.batch_size, self.window, self.dimensions = batch_size, window, dimensions
        self.queue = queue.Queue(maxsize=2 * batch_size)
        self.lock, self.closed = threading.Lock(), False
        # Inherit attempt accounting only, never a source fence or image scope.
        self.thread = threading.Thread(target=metrics.inherit(self._run, purpose_name='other'),
                                       daemon=True, name='clip-image')
        self.thread.start()

    def submit(self, tensor):
        future = Future()
        started = time.monotonic()
        with self.lock:
            if self.closed:
                raise RuntimeError('CLIP batcher is closed')
            self.queue.put((tensor, future, time.monotonic()))
        try:
            return future.result()
        finally:
            metrics.add(clip_image_requests=1,
                        clip_image_request_ms=(time.monotonic() - started) * 1000)

    def _valid(self, row):
        return (isinstance(row, list) and len(row) == self.dimensions
                and all(isinstance(v, (float, int)) and math.isfinite(v) for v in row)
                and abs(sum(v * v for v in row) - 1) < .001)

    def _run(self):
        while True:
            first = self.queue.get()
            if first is None:
                return
            batch, stop = [first], False
            deadline = time.monotonic() + self.window
            while len(batch) < self.batch_size:
                try:
                    item = self.queue.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            started = time.monotonic()
            metrics.add(clip_image_batches=1, clip_image_batch_items=len(batch),
                        clip_image_singleton_batches=int(len(batch) == 1),
                        clip_image_queue_ms=sum((started - request[2]) * 1000 for request in batch))
            try:
                # Avoid stacking/copying for a singleton flush.
                result = ([self.fallback(batch[0][0])] if len(batch) == 1
                          else self.encode([tensor for tensor, _, _ in batch]))
            except Exception:
                result = []
            valid = (isinstance(result, list) and len(result) == len(batch)
                     and all(self._valid(row) for row in result))
            if not valid:
                metrics.add(clip_image_batch_fallbacks=len(batch))
            for index, (tensor, future, _) in enumerate(batch):
                try:
                    vector = result[index] if valid else self.fallback(tensor)
                    if not self._valid(vector):
                        raise RuntimeError('Invalid CLIP image embedding')
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(vector)
            metrics.add(clip_image_batch_service_ms=(time.monotonic() - started) * 1000)
            # Do not retain pixels, tensors, vectors or request futures while idle.
            first = batch = result = tensor = future = vector = item = None
            if stop:
                return

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.queue.put(None)
        self.thread.join()


@contextmanager
def image_batches(encode, fallback, **options):
    if _ACTIVE.get() is not None:
        yield
        return
    batcher = ImageBatcher(encode, fallback, **options)
    token = _ACTIVE.set(batcher)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        batcher.close()  # Caller drains source adapters before exiting this scope.


def current_batcher():
    return _ACTIVE.get()
