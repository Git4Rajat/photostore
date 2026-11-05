"""Bounded key queries; reads keep SDK entity ETags for conditional writers."""
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, wait
from itertools import groupby
import threading
import time

from azure.core.exceptions import ResourceNotFoundError
from processing_metrics import add, observe, inherit

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='table-read')
_SLOTS = threading.BoundedSemaphore(4)
_FAMILIES = ('deleted-source!', 'supersession!', 'settlement!', 'admitted-source!',
             'source-completion!', 'manifest!', 'face!')


def key_batches(keys):
    batch = []
    for key in keys:
        candidate = batch + [key]
        # A partition comparison plus fourteen key comparisons is the Table
        # filter limit. Bound URL size too, including non-ASCII source names.
        if batch and (len(batch) == 14 or len(quote(str(candidate))) > 5000):
            yield batch
            batch = []
        batch.append(key)
    if batch:
        yield batch


def query_batches(keys, *, group_namespaces=False):
    ordered = sorted(set(keys))
    if not group_namespaces:
        yield from key_batches(ordered)
        return
    def family(key):
        return next((prefix for prefix in _FAMILIES if key.startswith(prefix)), '')
    for _, grouped in groupby(sorted(ordered, key=lambda key: (family(key), key)), key=family):
        yield from key_batches(grouped)


def read_entities(table, partition, keys, *, workers=1, group_namespaces=False, strategy='query'):
    """Return just requested keys, including None for authoritative absence.

    Callers bound their input. Known coordination families never share a query
    when requested. Returned entities retain their original SDK ETags.
    """
    keys = list(dict.fromkeys(keys))
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('Table read concurrency must be between one and four')
    if strategy not in ('query', 'point'):
        raise ValueError('Unknown table read strategy')
    result = dict.fromkeys(keys)
    if keys: add(batch_requests=1, batch_keys=len(keys))
    use_points = strategy == 'point' or not hasattr(table, 'query_entities')
    escaped = lambda value: value.replace("'", "''")
    started = time.monotonic()
    batches = [[key] for key in keys] if use_points else list(query_batches(keys, group_namespaces=group_namespaces))
    active, active_lock = [0], threading.Lock()
    def fetch(batch, submitted):
        with _SLOTS:
            with active_lock:
                active[0] += 1
                observe(batch_peak_inflight=active[0])
            add(batch_queue_ms=(time.monotonic() - submitted) * 1000)
            service_started = time.monotonic()
            try:
                return query(batch)
            except BaseException:
                add(**{'batch_point_errors' if use_points else 'batch_query_errors': 1})
                raise
            finally:
                add(batch_service_ms=(time.monotonic() - service_started) * 1000)
                with active_lock:
                    active[0] -= 1
    def query(batch):
        if use_points:
            key = batch[0]
            add(batch_point_reads=1)
            try:
                row = table.get_entity(partition, key)
            except ResourceNotFoundError:
                return {}
            if row.get('PartitionKey') != partition or row.get('RowKey') != key:
                raise ValueError('Point read returned another entity')
            return {key: row}
        add(batch_queries=1)
        wanted = set(batch)
        rows = {}
        query = "PartitionKey eq '" + escaped(partition) + "' and (" + ' or '.join(
            "RowKey eq '" + escaped(key) + "'" for key in batch) + ')'
        for row in table.query_entities(query):
            if row.get('PartitionKey') != partition:
                raise ValueError('Batch read returned another partition')
            if row.get('RowKey') in wanted:
                rows[row['RowKey']] = row
        return rows
    try:
        for offset in range(0, len(batches), workers):
            group = batches[offset:offset + workers]
            if workers == 1:
                result.update(fetch(group[0], time.monotonic()))
            else:
                futures = [_POOL.submit(inherit(lambda batch=batch, submitted=time.monotonic():
                    fetch(batch, submitted), purpose_name=None)) for batch in group]
                try:
                    for future in futures:
                        result.update(future.result())
                finally:
                    wait(futures)
    finally:
        add(batch_wall_ms=(time.monotonic() - started) * 1000)
    add(batch_rows=sum(row is not None for row in result.values()), batch_missing=sum(row is None for row in result.values()))
    return result
