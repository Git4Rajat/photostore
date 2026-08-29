"""In-memory fakes of azure.data.tables clients for unit tests.

Supports only the surface library_utils / password_auth use: upsert_entity,
get_entity, query_entities("PartitionKey eq 'X'" [and one more "field eq 'Y'"
clause]), delete_entity, create_table.
"""
from __future__ import annotations

import re


class ResourceNotFound(Exception):
    pass


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict = {}  # (pk, rk) -> dict

    def create_table(self):
        pass

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str):
        # Compound clause checked first -- the plain-PartitionKey pattern's
        # greedy '(.*)' would otherwise swallow " and field eq '...'" as part
        # of the partition key value itself.
        m = re.match(r"PartitionKey eq '(.*)' and (\w+) eq '(.*)'$", filter_str.strip())
        if m:
            pk, field, value = m.group(1), m.group(2), m.group(3)
            return [
                dict(v) for (p, _), v in self.rows.items()
                if p == pk and str(v.get(field, '')) == value
            ]
        m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
        if m:
            pk = m.group(1)
            return [dict(v) for (p, _), v in self.rows.items() if p == pk]
        raise ValueError(f'Unsupported filter: {filter_str}')


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
