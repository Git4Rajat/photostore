"""Bounded source-local reuse of proven immutable receipt writes only."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

import processing_metrics as metrics

_ACTIVE = ContextVar('committed_processing_receipts', default=None)
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 256


class CommittedReceipts:
    def __init__(self, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
        self.max_bytes, self.max_rows = max_bytes, max_rows
        self.rows, self.bytes = OrderedDict(), 0

    def remember(self, table, row):
        partition, key, payload = (row[k] for k in ('PartitionKey', 'RowKey', 'payload'))
        if not key.startswith(('face!', 'manifest!')):
            return
        # Count serialized strings plus a fixed row allowance, and bound entry
        # count separately. This is not a total Python/native RSS measurement.
        size = sum(len(v.encode('utf-8')) for v in (partition, key, payload)) + 256
        if size > self.max_bytes or self.max_rows < 1:
            metrics.add(receipt_commit_cache_oversize=1)
            return
        identity = (id(table), partition, key)
        old = self.rows.pop(identity, None)
        if old is not None:
            self.bytes -= old[-1]
        while self.rows and (self.bytes + size > self.max_bytes or len(self.rows) >= self.max_rows):
            self.bytes -= self.rows.popitem(last=False)[1][-1]
            metrics.add(receipt_commit_cache_evictions=1)
        self.rows[identity] = (table, partition, key, payload, size)
        self.bytes += size
        metrics.observe(receipt_commit_cache_peak_bytes=self.bytes,
                        receipt_commit_cache_peak_rows=len(self.rows))

    def get(self, table, partition, key):
        value = self.rows.get((id(table), partition, key))
        if value is None:
            metrics.add(receipt_commit_cache_misses=1)
            return None
        self.rows.move_to_end((id(table), partition, key))
        metrics.add(receipt_commit_cache_hits=1)
        return dict(PartitionKey=partition, RowKey=key, payload=value[3])

    def snapshot(self):
        # Strings are immutable, and the strong client reference prevents id()
        # reuse. Only a completed source exports these bounded rows.
        return tuple(value[:4] for value in self.rows.values())

    def clear(self):
        self.rows.clear()
        self.bytes = 0


@contextmanager
def committed_receipt_scope():
    cache = CommittedReceipts()
    token = _ACTIVE.set(cache)
    try:
        yield cache
    finally:
        _ACTIVE.reset(token)
        cache.clear()


def remember_committed(table, row):
    cache = _ACTIVE.get()
    if cache is not None:
        cache.remember(table, row)


def cached_receipt(table, partition, key):
    cache = _ACTIVE.get()
    return cache.get(table, partition, key) if cache is not None else None
