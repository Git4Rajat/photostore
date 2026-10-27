"""Bounded key queries; reads keep SDK entity ETags for conditional writers."""
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from processing_metrics import add


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


def read_entities(table, partition, keys):
    """Return just requested keys, including None for authoritative absence.

    Callers bound their input. No full partition scan or process-wide cache.
    """
    keys = list(dict.fromkeys(keys))
    result = dict.fromkeys(keys)
    if keys: add(batch_requests=1, batch_keys=len(keys))
    if not hasattr(table, 'query_entities'):
        for key in keys:
            add(batch_point_reads=1)
            try:
                result[key] = table.get_entity(partition, key)
            except ResourceNotFoundError:
                pass
        add(batch_rows=sum(row is not None for row in result.values()), batch_missing=sum(row is None for row in result.values()))
        return result
    escaped = lambda value: value.replace("'", "''")
    for batch in key_batches(keys):
        add(batch_queries=1)
        wanted = set(batch)
        query = "PartitionKey eq '" + escaped(partition) + "' and (" + ' or '.join(
            "RowKey eq '" + escaped(key) + "'" for key in batch) + ')'
        for row in table.query_entities(query):
            if row.get('PartitionKey') != partition:
                raise ValueError('Batch read returned another partition')
            if row.get('RowKey') in wanted:
                result[row['RowKey']] = row
    add(batch_rows=sum(row is not None for row in result.values()), batch_missing=sum(row is None for row in result.values()))
    return result
