"""Source-scoped conditional face writes; publish filename completeness last.

Transactions are per table/partition, never cross-table atomic. Failure leaves
the filename lookup unqualified; callers retain their normal recovery path.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import time

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode
import processing_metrics as metrics

_ACTIVE = ContextVar('face_conditional_write_batch', default=None)


def enabled():
    return os.getenv('IPWORK_FACE_PERSIST_BATCH_ENABLED', 'true').lower() in ('true', '1', 'yes')


@contextmanager
def face_write_batch(use, guard):
    token = _ACTIVE.set({'tables': {}, 'guard': guard} if use else None)
    try:
        yield
        if _ACTIVE.get() and _ACTIVE.get()['tables']:
            raise RuntimeError('Face writes must flush before filename lookup publication')
    finally:
        _ACTIVE.reset(token)


def enqueue(table, entity, fence, previous, previous_known):
    batch = _ACTIVE.get()
    if batch is None or not hasattr(table, 'submit_transaction'):
        return False
    if not previous_known:
        try: previous = table.get_entity(entity['PartitionKey'], entity['RowKey'])
        except ResourceNotFoundError: previous = None
    row = dict(entity, **fence.headers())
    if previous is None:
        operation = ('create', row)
    else:
        etag = getattr(previous, 'metadata', {}).get('etag')
        if not etag or etag == '*':
            from processing_completion import StaleProcessingResult
            raise StaleProcessingResult('Face batch requires genuine artifact ETags')
        operation = ('update', row, dict(mode=UpdateMode.REPLACE, etag=etag,
                                      match_condition=MatchConditions.IfNotModified))
    bucket = batch['tables'].setdefault((id(table), row['PartitionKey']), [table, {}])[1]
    if row['RowKey'] in bucket:
        raise ValueError('Duplicate face artifact in one publication batch')
    bucket[row['RowKey']] = operation
    return True


def flush():
    batch = _ACTIVE.get()
    if batch is None: return
    from processing_fence import current_fence
    for table, keyed in batch['tables'].values():
        operations = list(keyed.values())
        # Conservative size bound below Azure's 4 MiB transaction body limit.
        pages, page, size = [], [], 0
        import json
        for operation in operations:
            estimate = len(json.dumps(operation[1], ensure_ascii=True, default=str).encode()) * 2 + 2048
            if estimate > 3_000_000: raise ValueError('Oversized face artifact batch row')
            if page and (len(page) >= 100 or size + estimate > 3_000_000):
                pages.append(page); page, size = [], 0
            page.append(operation); size += estimate
        if page: pages.append(page)
        for page in pages:
            batch['guard']()  # Filename writer must still own its live lease.
            current_fence().check()
            started = time.monotonic()
            try: table.submit_transaction(page)
            except Exception:
                metrics.add(face_write_batch_errors=1)
                raise
            finally: metrics.add(face_write_batch_ms=(time.monotonic()-started)*1000)
            metrics.add(face_write_batches=1, face_write_batch_rows=len(page))
    batch['tables'].clear()
