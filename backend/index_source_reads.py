"""Bounded, ordered source reads for incremental index maintenance.

Only reads run in the pool. Validation, SQLite changes and publication stay on
the caller's thread. Copy each task's context so accounting and fences follow
the read, as they do for concurrent wave preparation.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os


class SourceReads:
    def __init__(self, workers=None):
        self.workers = int(os.getenv('INDEX_SOURCE_READ_CONCURRENCY', '4')) if workers is None else workers
        if type(self.workers) is not int or not 1 <= self.workers <= 16:
            raise ValueError('INDEX_SOURCE_READ_CONCURRENCY must be between 1 and 16')
        self.pool = None

    def __enter__(self):
        if self.workers > 1:
            self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='index-source')
        return self

    def __exit__(self, *exc):
        if self.pool is not None:
            # Drain running reads before leaving a publication/lease context.
            self.pool.shutdown(wait=True, cancel_futures=True)

    def map(self, read, items):
        """Yield (key, row) in input order, prefetching at most workers per iterator.

        A merge may nest person and face iterators on this same pool. Workers
        never wait for another task; only the owner consumes the results.
        """
        if self.pool is None:
            for item in items:
                yield item, read(item)
            return
        pending = deque()
        items = iter(items)

        def submit():
            try:
                item = next(items)
            except StopIteration:
                return False
            pending.append((item, self.pool.submit(copy_context().run, read, item)))
            return True

        try:
            for _ in range(self.workers):
                if not submit():
                    break
            while pending:
                item, future = pending.popleft()
                yield item, future.result()
                submit()
        finally:
            for _, future in pending:
                future.cancel()
