"""ETag-enforcing lifecycle table with atomic create/update/delete batches."""
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from test_upload_waves import AtomicTable


class RegistryTable(AtomicTable):
    def query_entities(self, query, **options):
        import re
        limit = options.get('results_per_page', 1000)
        rows = list(super().query_entities(query, results_per_page=100000))
        if 'read eq false' in query:
            rows = [row for row in rows if not row.get('read')]
        match = re.search(r'expiresAt (le|gt) ([0-9.]+)', query)
        if match:
            op, value = match[1], float(match[2])
            rows = [row for row in rows if (row['expiresAt']<=value if op=='le' else row['expiresAt']>value)]
        return iter(rows[:limit])

    def submit_transaction(self, operations):
        operations = list(operations)
        hook, self.before_commit = self.before_commit, None
        if hook:
            hook()
        with self.lock:
            assert 1 <= len(operations) <= 100
            assert len({op[1]['PartitionKey'] for op in operations}) == 1
            for kind, row, *options in operations:
                key = row['PartitionKey'], row['RowKey']
                if kind == 'create' and key in self.rows:
                    raise ResourceExistsError('exists')
                if kind in ('update', 'delete'):
                    if key not in self.rows or str(self.versions[key]) != options[0]['etag']:
                        raise ResourceModifiedError('changed')
                    assert options[0]['match_condition'] is not None
            for kind, row, *options in operations:
                key = row['PartitionKey'], row['RowKey']
                if kind == 'delete':
                    del self.rows[key]
                else:
                    self.rows[key] = dict(row)
                    self.versions[key] = self.versions.get(key, 0) + 1
            self.transactions.append(operations)
