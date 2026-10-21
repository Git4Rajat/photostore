"""In-memory fakes of azure.data.tables clients for unit tests.

Supports only the surface library_utils / password_auth use: upsert_entity,
get_entity, query_entities("PartitionKey eq 'X'" [and one more "field eq 'Y'"
clause]), delete_entity, create_table.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError


class ResourceNotFound(ResourceNotFoundError):
    pass


class _Entity(dict):
    def __init__(self, row, version):
        super().__init__(row)
        self.metadata = {'etag': str(version)}


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict = {}  # (pk, rk) -> dict
        self.stamps: dict = {}  # (pk, rk) -> datetime of the last write (the service-side Timestamp)
        self.versions: dict = {}
        self.clock = None  # tests may set a callable returning a datetime
        self.submit_transaction_calls: list = []  # list of the operations lists passed in

    def create_table(self):
        pass

    def upsert_entity(self, entity, mode=None, **kwargs):
        key = entity['PartitionKey'], entity['RowKey']
        self.versions[key] = self.versions.get(key, 0) + 1
        self.rows[key] = dict(entity)
        self.stamps[(entity['PartitionKey'], entity['RowKey'])] = self.clock() if self.clock else datetime.now(timezone.utc)

    def submit_transaction(self, operations):
        # Real azure-data-tables requires every entity in one transaction to
        # share a PartitionKey and caps it at 100 operations -- not enforced
        # here since nothing in this test suite needs that failure mode, only
        # the "N upserts become 1 call" behavior storage_utils's dirty-
        # filename batching relies on (see _flush_dirty_filename_buffer).
        self.submit_transaction_calls.append(list(operations))
        for operation in operations:
            op_type, entity = operation[:2]
            kwargs = operation[2] if len(operation) > 2 else {}
            if op_type == 'upsert':
                self.upsert_entity(entity, **kwargs)
            elif op_type == 'delete':
                self.delete_entity(entity['PartitionKey'], entity['RowKey'], **kwargs)
            else:
                raise ValueError(f'Unsupported fake transaction op: {op_type}')

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        # Match conditional writes, including projected MERGE entities.
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise ResourceNotFound(f'{key} not found')
        expected = etag or getattr(entity, 'metadata', {}).get('etag')
        if match_condition is not None and expected != str(self.versions.get(key, 0)):
            raise ResourceModifiedError('changed')
        self.rows[key] = {**self.rows[key], **entity} if mode == 'merge' else dict(entity)
        self.versions[key] = self.versions.get(key, 0) + 1

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFound(f'{key} not found')
        return _Entity(self.rows[key], self.versions.get(key, 0))

    def delete_entity(self, partition_key, row_key, *, etag=None, match_condition=None):
        key = partition_key, row_key
        if match_condition is not None and etag != str(self.versions.get(key, 0)):
            raise ResourceModifiedError('changed')
        self.rows.pop(key, None)

    def _project(self, rows, select):
        for i, row in enumerate(rows):
            rows[i] = _Entity({k: v for k, v in row.items() if not select or k in select},
                              self.versions.get((row.get('PartitionKey'), row.get('RowKey')), 0))
        return rows

    def query_entities(self, filter_str, select=None, **kwargs):
        # Compound clause checked first -- the plain-PartitionKey pattern's
        # greedy '(.*)' would otherwise swallow " and field eq ..." as part
        # of the partition key value itself. Second alternative covers
        # unquoted OData boolean literals (e.g. "processing_complete eq
        # true"), which don't fit the quoted-string '(.*)' form.
        m = re.match(r"PartitionKey eq '([^']*)' and Timestamp ge datetime'([^']+)'$", filter_str.strip())
        if m:
            pk, since = m.group(1), datetime.strptime(m.group(2), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            rows = [dict(v) for (p, r), v in self.rows.items() if p == pk and self.stamps.get((p, r), since) >= since]
            return self._project(rows, select)
        m = re.match(r"PartitionKey eq '([^']*)' and \((.+)\)$", filter_str.strip())
        if m:
            # "PartitionKey eq 'x' and (a ge 'v' or b eq 'w' or ...)"
            pk = m.group(1)
            clauses = []
            for part in m.group(2).split(' or '):
                cm = re.match(r"(\w+) (eq|ge|le|gt|lt) '(.*)'$", part.strip())
                if not cm:
                    raise ValueError(f'Unsupported filter: {filter_str}')
                clauses.append(cm.groups())
            ops = {
                'eq': lambda a, b: a == b, 'ge': lambda a, b: a >= b, 'le': lambda a, b: a <= b,
                'gt': lambda a, b: a > b, 'lt': lambda a, b: a < b,
            }
            rows = [
                dict(v) for (p, _), v in self.rows.items()
                if p == pk and any(ops[op](str(v.get(f, '')), val) for f, op, val in clauses)
            ]
            return self._project(rows, select)
        m = re.match(r"PartitionKey eq '(.*)' and (\w+) eq '(.*)'$", filter_str.strip())
        if m:
            pk, field, value = m.group(1), m.group(2), m.group(3)
            rows = [
                dict(v) for (p, _), v in self.rows.items()
                if p == pk and str(v.get(field, '')) == value
            ]
        elif (m := re.match(r"PartitionKey eq '(.*)' and (\w+) eq (true|false)$", filter_str.strip())):
            pk, field, value = m.group(1), m.group(2), m.group(3) == 'true'
            rows = [
                dict(v) for (p, _), v in self.rows.items()
                if p == pk and bool(v.get(field, False)) == value
            ]
        else:
            m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
            if not m:
                raise ValueError(f'Unsupported filter: {filter_str}')
            pk = m.group(1)
            rows = [dict(v) for (p, _), v in self.rows.items() if p == pk]
        return self._project(rows, select)


def make_store():
    import library_utils

    return library_utils.LibraryStore(
        users_table=FakeTable(),
        libraries_table=FakeTable(),
        memberships_table=FakeTable(),
        invites_table=FakeTable(),
        audit_table=FakeTable(),
        clean_requests_table=FakeTable(),
    )
