"""Attempt-scoped, bounded text inference batches; source writes stay on callers."""
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
import queue
import threading
import time

import processing_metrics as metrics

_ACTIVE = ContextVar('semantic_text_batcher', default=None)


class TextBatcher:
    def __init__(self, encode, fallback, *, window=.005, batch_size=8):
        self.encode, self.fallback = encode, fallback
        self.window, self.batch_size = window, batch_size
        self.queue = queue.Queue(maxsize=32)
        # Only attempt-level accounting is inherited, never source fences or
        # image counters. Per-request elapsed is accounted on each caller.
        self.thread = threading.Thread(target=metrics.inherit(self._run, purpose_name='semantic_refresh'),
                                       daemon=True, name='semantic-text')
        self.thread.start()

    def submit(self, text):
        future = Future()
        started = time.monotonic()
        self.queue.put((text, future))
        try:
            return future.result()
        finally:
            metrics.add(semantic_text_request_ms=(time.monotonic() - started) * 1000,
                        semantic_text_requests=1)

    def _run(self):
        while True:
            first = self.queue.get()
            if first is None: return
            batch, stop = [first], False
            deadline = time.monotonic() + self.window
            while len(batch) < self.batch_size:
                try: item = self.queue.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty: break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            metrics.add(semantic_text_batches=1, semantic_text_batch_items=len(batch))
            try:
                result = self.encode([text for text, _ in batch])
            except Exception:
                result = []
            valid = (isinstance(result, list) and len(result) == len(batch)
                     and all(isinstance(row, list) and row for row in result))
            if not valid:
                metrics.add(semantic_text_batch_fallbacks=len(batch))
            for index, (text, future) in enumerate(batch):
                try:
                    vector = result[index] if valid else self.fallback(text)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(vector)
            # Idle worker must not retain texts, vectors, request futures or
            # exception tracebacks after handing results back to source threads.
            first = batch = result = item = future = vector = text = None
            if stop: return

    def close(self):
        self.queue.put(None)
        self.thread.join()


@contextmanager
def text_batches(encode, fallback, **options):
    if _ACTIVE.get() is not None:
        yield
        return
    batcher = TextBatcher(encode, fallback, **options)
    token = _ACTIVE.set(batcher)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        batcher.close()  # Source adapters have drained before this scope exits.


def current_batcher():
    return _ACTIVE.get()
