import threading
import re

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from index_work import DirtySnapshot, WorkStore, WakeupContention, acknowledge_dirty


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
        if 'failed eq true' in query:
            rows = [row for row in rows if row.get('failed')]
        for field in ('leaseUntil', 'wakeUntil', 'requestedAt', 'failedAt'):
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


def test_notify_raises_a_distinct_type_for_reservation_contention(work):
    store, _, _ = work
    key = store.request('contended', 'people')
    monkeypatched_replace_calls = []
    def always_conflicts(current, row):
        monkeypatched_replace_calls.append(1)
        raise ResourceModifiedError('conflict')
    store._replace = always_conflicts
    with pytest.raises(WakeupContention):
        store.notify(key, lambda *a: None)
    assert len(monkeypatched_replace_calls) == 12


def test_reservation_contention_does_not_abort_the_recover_batch(work):
    store, _, _ = work
    store.request('contended', 'people')
    store.request('healthy', 'people')
    original_replace = store._replace
    def conflict_only_for_contended(current, row):
        if row.get('libraryId') == 'contended':
            raise ResourceModifiedError('conflict')
        return original_replace(current, row)
    store._replace = conflict_only_for_contended
    sent = []
    assert store.recover(lambda library, *rest: sent.append(library)) == 1
    assert sent == ['healthy']


def test_one_poison_row_does_not_abort_the_rest_of_the_batch(work):
    store, _, _ = work
    store.request('poison', 'people')
    store.request('healthy', 'people')
    sent = []
    def send(library, *rest):
        if library == 'poison':
            raise RuntimeError('queue unavailable for this stage')
        sent.append(library)
    assert store.recover(send) == 1
    assert sent == ['healthy']


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


def test_retry_timing_is_piggybacked_consumed_once_and_invalidated_by_new_revision(work):
    store, table, now = work
    key = store.request('A', 'wave-vision:wave:outbox')
    first = store.claim(key)
    calls = []
    original_get, original_update = table.get_entity, table.update_entity
    table.get_entity = lambda *a: (calls.append('get'), original_get(*a))[1]
    table.update_entity = lambda *a, **kw: (calls.append('update'), original_update(*a, **kw))[1]
    store.release(first, retry_delay=2, retry_reason='coordination_conflict')
    assert calls == ['get', 'update']  # No telemetry read or write.
    now[0] += 7
    second = store.claim(key)
    assert second.scheduled_at == 1000 and second.scheduled_delay == 2
    assert second.schedule_id and second.schedule_reason == 'coordination_conflict'
    store.release(second)
    third = store.claim(key)
    assert third.schedule_id == ''  # Duplicate delivery cannot reuse old timing.
    store.release(third, retry_delay=2)
    store.request('A', 'wave-vision:wave:outbox')
    fourth = store.claim(key)
    assert fourth.schedule_id == ''  # A newer requested revision has another origin.


def test_malformed_optional_retry_telemetry_cannot_block_a_claim(work):
    store, table, _ = work
    key = store.request('A', 'scope')
    current = store._get(key)
    table.update_entity(dict(current, retryScheduledAt='invalid', retryDelaySeconds='invalid',
        retryScheduleRevision=current['revision'], retryScheduleId='invalid'), etag=current.metadata['etag'])
    claim = store.claim(key)
    assert claim and not claim.schedule_id
