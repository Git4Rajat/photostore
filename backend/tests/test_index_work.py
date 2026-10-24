import threading
import re

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from index_work import DirtySnapshot, WorkStore, acknowledge_dirty


class Entity(dict):
    def __init__(self, data, version):
        super().__init__(data)
        self.metadata = {'etag': str(version)}


class EtagTable:
    def __init__(self):
        self.rows = {}
        self.versions = {}
        self.lock = threading.Lock()

    def get_entity(self, partition_key, row_key):
        with self.lock:
            key = partition_key, row_key
            if key not in self.rows:
                raise ResourceNotFoundError('missing')
            return Entity(self.rows[key], self.versions[key])

    def create_entity(self, row):
        with self.lock:
            key = row['PartitionKey'], row['RowKey']
            if key in self.rows:
                raise ResourceExistsError('exists')
            self.rows[key] = dict(row)
            self.versions[key] = self.versions.get(key, 0) + 1

    def upsert_entity(self, row, **kwargs):
        with self.lock:
            key = row['PartitionKey'], row['RowKey']
            self.rows[key] = dict(row)
            self.versions[key] = self.versions.get(key, 0) + 1

    def update_entity(self, row, *, etag, **kwargs):
        with self.lock:
            key = row['PartitionKey'], row['RowKey']
            if str(self.versions.get(key)) != etag:
                raise ResourceModifiedError('changed')
            self.rows[key] = dict(row)
            self.versions[key] += 1

    def delete_entity(self, partition_key, row_key, *, etag, **kwargs):
        with self.lock:
            key = partition_key, row_key
            if key not in self.rows:
                raise ResourceNotFoundError('missing')
            if str(self.versions[key]) != etag:
                raise ResourceModifiedError('changed')
            del self.rows[key]

    def query_entities(self, query, **kwargs):
        pk = query.split("'")[1]
        with self.lock:
            rows = [Entity(row, self.versions[key]) for key, row in self.rows.items() if key[0] == pk]
        if ' and libraryId eq ' in query:
            library = query.split(' and libraryId eq ')[1].split("'")[1]
            rows = [row for row in rows if row.get('libraryId') == library]
        if 'failed eq false' in query:
            rows = [row for row in rows if not row.get('failed')]
        for field in ('leaseUntil', 'wakeUntil'):
            match = re.search(rf'{field} le ([0-9.]+)', query)
            if match:
                rows = [row for row in rows if float(row.get(field) or 0) <= float(match[1])]
        return rows


@pytest.fixture
def work():
    table = EtagTable()
    now = [1000.0]
    return WorkStore(table, clock=lambda: now[0]), table, now


def test_reserved_work_cannot_starve_later_libraries(work):
    store, _, _ = work
    for i in range(105):
        key = store.request(f'busy-{i}', 'people')
        store.notify(key, lambda *args: None)
    store.request('ready', 'search')
    sent = []
    assert store.recover(lambda *args: sent.append(args), limit=1) == 1
    assert sent[0][0] == 'ready'


def test_dirty_ack_keeps_conditional_deletes_batched():
    from tests.fakes import FakeTable
    table = FakeTable()
    for i in range(201):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': str(i)})
    snapshot = DirtySnapshot(table.query_entities("PartitionKey eq 'A'"))
    acknowledge_dirty(table, snapshot)
    assert not table.rows
    assert [len(ops) for ops in table.submit_transaction_calls] == [100, 100, 1]
    assert all(op[2]['etag'] for ops in table.submit_transaction_calls for op in ops)


def test_dirty_batch_conflict_retries_conditionally_without_losing_the_edit():
    from tests.fakes import FakeTable
    table = FakeTable()
    for name in ('changed', 'unchanged'):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': name})
    snapshot = DirtySnapshot(table.query_entities("PartitionKey eq 'A'"))
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'changed'})
    acknowledge_dirty(table, snapshot)
    assert set(table.rows) == {('A', 'changed')}


def test_a_coalesced_request_still_advances_revision(work):
    store, _, _ = work
    key = store.request('A', 'people')
    sent = []
    assert store.notify(key, lambda *args: sent.append(args))
    claim = store.claim(key)
    store.request('A', 'people')
    assert not store.notify(key, lambda *args: sent.append(args))
    assert store.finish(claim)
    assert store.notify(key, lambda *args: sent.append(args))
    second = store.claim(key)
    assert second.revision != claim.revision
    assert not store.finish(second)
    assert store.claim(key) is None
    assert len(sent) == 2


def test_request_racing_completion_cannot_be_deleted(work, monkeypatch):
    store, table, _ = work
    key = store.request('A', 'search')
    claim = store.claim(key)
    original = table.delete_entity
    raced = []

    def delete(*args, **kwargs):
        if not raced:
            raced.append(True)
            store.request('A', 'search')
        return original(*args, **kwargs)

    monkeypatch.setattr(table, 'delete_entity', delete)
    assert store.finish(claim)
    assert store.claim(key).revision != claim.revision


def test_failed_queue_send_is_recoverable_without_notification_row(work):
    store, _, now = work
    key = store.request('A', 'people')
    with pytest.raises(OSError):
        store.notify(key, lambda *args: (_ for _ in ()).throw(OSError('queue down')))
    now[0] += 61
    sent = []
    assert store.recover(lambda *args: sent.append(args)) == 1
    assert sent[0][:2] == ('A', 'people')


def test_unexpired_lease_excludes_other_workers_and_expired_owner_cannot_ack(work):
    store, _, now = work
    key = store.request('A', 'people')
    first = store.claim(key)
    with pytest.raises(RuntimeError, match='leased'):
        store.claim(key)
    now[0] += 181
    second = store.claim(key)
    with pytest.raises(RuntimeError, match='lost'):
        store.finish(first)
    store.finish(second)


def test_renewal_preserves_a_new_request(work):
    store, _, now = work
    key = store.request('A', 'people')
    claim = store.claim(key)
    store.request('A', 'people')
    now[0] += 60
    store.renew(claim)
    now[0] += 121
    assert store.finish(claim)


def test_scope_and_library_are_isolated(work):
    store, _, _ = work
    keys = [store.request(lib, scope) for lib, scope in [('A', 'people'), ('B', 'people'), ('A', 'search')]]
    claims = [store.claim(key) for key in keys]
    assert len(set(keys)) == 3
    assert all(claims)
    for claim in claims:
        store.finish(claim)


def test_failure_budget_does_not_restart_until_new_request(work):
    store, _, _ = work
    key = store.request('A', 'people')
    claim = store.claim(key)
    store.release(claim)
    store.fail_revision(key, claim.revision)
    assert store.claim(key) is None
    assert store.recover(lambda *a: pytest.fail('poison retry')) == 0
    store.request('A', 'people')
    assert store.claim(key) is not None


def test_old_failure_cannot_poison_a_new_revision(work):
    store, _, _ = work
    key = store.request('A', 'people')
    claim = store.claim(key)
    store.request('A', 'people')
    store.release(claim)
    store.fail_revision(key, claim.revision)
    assert store.claim(key) is not None


def test_dirty_ack_preserves_newer_marks_and_new_entities():
    table = EtagTable()
    def mark(name):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': name})
    mark('one')
    mark('two')
    snapshot = DirtySnapshot(table.query_entities("PartitionKey eq 'A'"))
    mark('one')
    mark('three')
    acknowledge_dirty(table, snapshot)
    assert {key[1] for key in table.rows} == {'one', 'three'}


def test_ack_subset_does_not_clear_other_work():
    table = EtagTable()
    for name in ('one', 'two'):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': name})
    snapshot = DirtySnapshot(table.query_entities("PartitionKey eq 'A'"))
    acknowledge_dirty(table, snapshot.subset(['one']))
    assert {key[1] for key in table.rows} == {'two'}


def test_unversioned_snapshot_fails_closed():
    with pytest.raises(RuntimeError, match='ETag'):
        acknowledge_dirty(EtagTable(), DirtySnapshot([{'PartitionKey': 'A', 'RowKey': 'one'}]))


def test_healthy_deferred_barrier_waits_do_not_exhaust_work_attempts(work):
    store,table,now=work
    key=store.request('u','wave-vision:w:key')
    for _ in range(10):
        claim=store.claim(key)
        assert claim.attempt==1
        store.release(claim,deferred=True)
        now[0]+=200
    claim=store.claim(key)
    store.release(claim)  # A real error remains charged.
    now[0]+=200
    claim=store.claim(key)
    assert claim.attempt==2
    store.release(claim,deferred=True)
    now[0]+=200
    assert store.claim(key).attempt==2
