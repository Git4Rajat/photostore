"""Shadow protocol races against an atomic, ETag-enforcing Table fake."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading

import pytest
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from upload_waves import Conflict, MAX_RETRIES, ShadowWaves


class Entity(dict):
    def __init__(self, row, version):
        super().__init__(row)
        self.metadata = {'etag': str(version)}


class AtomicTable:
    def __init__(self):
        self.rows, self.versions, self.transactions, self.queries = {}, {}, [], []
        self.lock = threading.RLock()
        self.before_commit = None

    def get_entity(self, partition_key, row_key):
        with self.lock:
            key = partition_key, row_key
            if key not in self.rows:
                raise ResourceNotFoundError('missing')
            return Entity(self.rows[key], self.versions[key])

    def submit_transaction(self, operations):
        operations = list(operations)
        hook, self.before_commit = self.before_commit, None
        if hook:
            hook()
        with self.lock:
            assert 1 <= len(operations) <= 100
            assert len({op[1]['PartitionKey'] for op in operations}) == 1
            keys = [(op[1]['PartitionKey'], op[1]['RowKey']) for op in operations]
            assert len(set(keys)) == len(keys)
            assert len(json.dumps(operations, default=str).encode()) < 4 * 1024 * 1024
            for (op, row, *options), key in zip(operations, keys):
                if op == 'create' and key in self.rows:
                    raise ResourceExistsError('exists')
                if op == 'update':
                    if key not in self.rows or str(self.versions[key]) != options[0]['etag']:
                        raise ResourceModifiedError('changed')
                    assert options[0]['match_condition'] is not None
                assert op in ('create', 'update')
            for (_, row, *_), key in zip(operations, keys):
                self.rows[key] = dict(row)
                self.versions[key] = self.versions.get(key, 0) + 1
            self.transactions.append(operations)

    def query_entities(self, query, *, results_per_page=1000):
        self.queries.append((query, results_per_page))
        pk = re.search(r"PartitionKey eq '([^']+)'", query)[1]
        names = re.findall(r"RowKey eq '((?:[^']|'')*)'", query)
        if names:
            names = {name.replace("''", "'") for name in names}
            with self.lock:
                return iter([Entity(row, self.versions[key]) for key, row in sorted(self.rows.items())
                             if key[0] == pk and key[1] in names])
        lower = re.search(r"RowKey ge '([^']+)'", query)[1]
        upper = re.search(r"RowKey lt '([^']+)'", query)[1]
        after_match = re.search(r"RowKey gt '([^']+)'", query)
        with self.lock:
            return iter([Entity(row, self.versions[key]) for key, row in sorted(self.rows.items())
                         if key[0] == pk and lower <= key[1] < upper
                         and (not after_match or key[1] > after_match[1])
                         and ("predecessorWaveId eq '" not in query or row.get('predecessorWaveId') == re.search(r"predecessorWaveId eq '([^']+)'", query)[1])][:results_per_page])


@pytest.fixture
def world():
    table = AtomicTable()
    now = [1000.0]
    return ShadowWaves(table, 'library-A', clock=lambda: now[0]), table, now


def register_accepted(store, session='parent', *, photo='photo-1', revision='revision-1', item='item-1'):
    row = store.open_session(session, 1)
    store.register(session, [{'itemId': item, 'filename': 'original.jpg'}])
    resolved = store.resolve(session, item, outcome='accepted', photo_id=photo,
                             processing_revision=revision, filename='original.jpg', required_steps=['face'])
    return row['waveId'], resolved


def close(store, session, row):
    return store.close(session, row['manifestRevision'], 'close-' + session)


def finish_scan(store, wave, *, size=20):
    for _ in range(1000):
        result = store.evaluate(wave, page_size=size)
        if result['state'] == 'ready' or not result.get('evaluation'):
            return result
    pytest.fail('readiness scan did not converge')


def settle(store, wave, photo='photo-1', revision='revision-1'):
    return store.observe(wave, photo, revision, outcome='settled', evidence_id='proof-' + photo)


def test_overlapping_sessions_join_one_wave_and_both_must_close(world):
    store, _, _ = world
    wave, first = register_accepted(store)
    second_wave, second = register_accepted(store, 'second', photo='photo-2')
    assert second_wave == wave
    close(store, 'parent', first)
    settle(store, wave)
    assert store.evaluate(wave)['state'] == 'collecting'
    close(store, 'second', second)
    assert finish_scan(store, wave)['state'] == 'processing'
    settle(store, wave, 'photo-2')
    assert finish_scan(store, wave)['state'] == 'ready'


def test_whole_parent_selection_spans_900_file_transport_boundary(world):
    store, _, _ = world
    session = store.open_session('parent', 901)
    wave = session['waveId']
    for start in range(0, 900, 20):
        store.register('parent', [{'itemId': f'i{i}', 'filename': f'{i}.jpg'} for i in range(start, start + 20)])
        for i in range(start, start + 20):
            session = store.resolve('parent', f'i{i}', outcome='cancelled')
    with pytest.raises(Conflict, match='unresolved'):
        close(store, 'parent', session)
    assert store.evaluate(wave)['state'] == 'collecting'
    store.register('parent', [{'itemId': 'i900', 'filename': '900.jpg'}])
    session = store.resolve('parent', 'i900', outcome='cancelled')
    close(store, 'parent', session)
    assert finish_scan(store, wave)['state'] == 'ready'


def test_duplicate_members_are_counted_once_and_api_replays_do_not_advance(world):
    store, table, _ = world
    wave, first = register_accepted(store)
    _, second = register_accepted(store, 'second')
    assert store.status(wave)['memberCount'] == 1
    transactions = len(table.transactions)
    assert store.open_session('parent', 1)['sessionId'] == 'parent'
    store.register('parent', [{'itemId': 'item-1', 'filename': 'original.jpg'}])
    store.resolve('parent', 'item-1', outcome='accepted', photo_id='photo-1',
                  processing_revision='revision-1', filename='original.jpg', required_steps=['face'])
    assert len(table.transactions) == transactions
    close(store, 'parent', first)
    close(store, 'second', second)
    settle(store, wave)
    settle(store, wave)
    result = finish_scan(store, wave, size=1)
    assert result['state'] == 'ready' and result['memberCount'] == 1


def test_stale_close_and_unresolved_reservations_cannot_close(world):
    store, _, _ = world
    initial = store.open_session('parent', 1)
    registered = store.register('parent', [{'itemId': 'i', 'filename': 'x.jpg'}])
    with pytest.raises(Conflict, match='stale'):
        close(store, 'parent', initial)
    with pytest.raises(Conflict, match='unresolved'):
        close(store, 'parent', registered)


def test_close_is_idempotent_but_different_close_request_is_not(world):
    store, table, _ = world
    row = store.open_session('parent', 0)
    close(store, 'parent', row)
    count = len(table.transactions)
    close(store, 'parent', row)
    assert len(table.transactions) == count
    with pytest.raises(Conflict, match='another request'):
        store.close('parent', row['manifestRevision'], 'other-close')


def test_reopen_before_freeze_invalidates_paged_evaluation(world):
    store, _, _ = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    settle(store, wave)
    assert store.evaluate(wave, page_size=1)['evaluation']
    second = store.open_session('later', 0)
    assert second['waveId'] == wave
    assert store.evaluate(wave)['state'] == 'collecting'
    close(store, 'later', second)
    assert finish_scan(store, wave, size=1)['state'] == 'ready'


def test_admission_racing_freeze_is_included_before_or_after_never_lost(world):
    store, table, _ = world
    row = store.open_session('parent', 0)
    wave = row['waveId']
    close(store, 'parent', row)
    admitted = []
    table.before_commit = lambda: admitted.append(store.open_session('racing', 0))
    assert store.evaluate(wave)['state'] == 'collecting'
    assert admitted[0]['waveId'] == wave
    close(store, 'racing', admitted[0])
    assert finish_scan(store, wave)['state'] == 'ready'
    successor = store.open_session('successor', 0)
    assert successor['waveId'] != wave
    assert store.status(successor['waveId'])['predecessorWaveId'] == wave
    close(store, 'successor', successor)
    assert store.evaluate(successor['waveId'])['state'] == 'waiting_predecessor'
    assert store.status(successor['waveId'])['waitReason'] == 'predecessor_incomplete'


def test_processing_observation_racing_final_page_restarts_scan(world):
    store, table, _ = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    settle(store, wave)
    table.before_commit = lambda: store.observe(wave, 'photo-1', 'revision-1', outcome='pending',
                                               evidence_id='new-lease', outstanding_leases=1)
    result = store.evaluate(wave)
    assert result['state'] == 'processing'
    assert result['outstandingLeases'] == 1
    assert not store._get('shadow-cluster!' + wave)


def test_completion_requires_exact_member_revision_and_resolved_lease(world):
    store, _, _ = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    with pytest.raises(Conflict, match='Unknown'):
        settle(store, wave, revision='stale-revision')
    with pytest.raises(Conflict, match='lease'):
        store.observe(wave, 'photo-1', 'revision-1', outcome='settled',
                      evidence_id='p', outstanding_leases=1)
    assert finish_scan(store, wave)['outstandingMembers'] == 1


def test_failure_requires_explicit_exhausted_outcome(world):
    store, _, _ = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    with pytest.raises(ValueError):
        store.observe(wave, 'photo-1', 'revision-1', outcome='failed', evidence_id='p')
    store.observe(wave, 'photo-1', 'revision-1', outcome='failed_exhausted', evidence_id='exhausted-proof')
    result = finish_scan(store, wave)
    assert result['state'] == 'ready' and result['failedMembers'] == 1


def test_scan_survives_store_restart_and_queries_are_bounded(world):
    store, table, now = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    settle(store, wave)
    table.queries.clear()  # Inspect readiness scans, not admission's bounded keyed reads.
    store.evaluate(wave, page_size=1)
    restarted = ShadowWaves(table, 'library-A', clock=lambda: now[0])
    result = finish_scan(restarted, wave, size=1)
    assert result['state'] == 'ready'
    assert all(size == 1 and 'RowKey ge' in query and 'RowKey lt' in query for query, size in table.queries)
    outbox = store._get('shadow-cluster!' + wave)
    assert outbox['dispatchable'] is False
    assert outbox['inputRevision'] == result['frozenRevision']
    assert store.evaluate(wave)['frozenRevision'] == result['frozenRevision']
    with pytest.raises(Conflict, match='Frozen'):
        store.observe(wave, 'photo-1', 'revision-1', outcome='pending', evidence_id='later')


def test_no_work_freeze_and_outbox_are_atomic_on_storage_failure(world):
    store, table, _ = world
    row = store.open_session('parent', 0)
    close(store, 'parent', row)
    def crash():
        raise AzureError('injected failure before transaction commit')
    table.before_commit = crash
    with pytest.raises(AzureError):
        store.evaluate(row['waveId'])
    assert store.status(row['waveId'])['state'] == 'processing_pending'
    assert store._get('shadow-cluster!' + row['waveId']) is None
    assert store.evaluate(row['waveId'])['state'] == 'ready'


def test_concurrent_sessions_share_control_and_do_not_lose_counts(world):
    store, _, _ = world
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda i: store.open_session('p' + str(i), 0), range(12)))
    assert len({row['waveId'] for row in rows}) == 1
    wave = rows[0]['waveId']
    assert store.status(wave)['sessionCount'] == store.status(wave)['openSessions'] == 12


def test_cached_counts_cannot_replace_manifest_evidence(world):
    store, table, _ = world
    row = store.open_session('parent', 0)
    close(store, 'parent', row)
    table.rows[store.partition, store.wave_key(row['waveId'])]['memberCount'] = 1
    with pytest.raises(Conflict, match='counts disagree'):
        store.evaluate(row['waveId'])


def test_library_isolation_and_age_metrics(world):
    store, table, now = world
    row = store.open_session('parent', 0)
    close(store, 'parent', row)
    now[0] += 86400
    assert store.status(row['waveId'])['ageSeconds'] == 86400
    assert store.status(row['waveId'])['barrierSeconds'] == 86400
    other = ShadowWaves(table, 'library-B')
    with pytest.raises(Conflict):
        other.status(row['waveId'])
    assert other.open_session('parent', 0)['waveId'] != row['waveId']


@pytest.mark.parametrize('session,count', [("bad'filter", 0), ('a', True), ('a', -1), ('a', 1000001)])
def test_invalid_declarations_do_not_write(world, session, count):
    store, table, _ = world
    with pytest.raises(ValueError):
        store.open_session(session, count)
    assert not table.rows


def test_intake_close_and_vision_marker_commit_together_and_reopen_invalidates_epoch(world):
    store, table, _ = world
    parent = store.open_session('parent', 0)
    wave = parent['waveId']
    def crash():
        raise AzureError('close transaction failed')
    table.before_commit = crash
    with pytest.raises(AzureError):
        close(store, 'parent', parent)
    assert store.status(wave)['state'] == 'collecting'
    assert store._required(store.session_key('parent'))['state'] == 'open'
    close(store, 'parent', parent)
    closed_wave = store.status(wave)
    marker = store._required(closed_wave['visionPendingKey'])
    assert not marker['dispatchable'] and marker['intakeEpoch'] == closed_wave['intakeEpoch']
    later = store.open_session('later', 0)
    assert store.status(wave)['state'] == 'collecting'
    assert store.status(wave)['intakeEpoch'] != marker['intakeEpoch']
    close(store, 'later', later)
    assert store.status(wave)['visionPendingKey'] != marker['RowKey']


def test_retry_exhaustion_cannot_partially_commit_intake(world):
    store, table, _ = world
    attempts = []
    original = table.submit_transaction
    def conflicted(ops):
        attempts.append(1)
        raise ResourceModifiedError('forced conflict')
    table.submit_transaction = conflicted
    with pytest.raises(Conflict, match='Concurrent'):
        store.open_session('parent', 0)
    assert len(attempts) == MAX_RETRIES and not table.rows
    table.submit_transaction = original
    assert store.open_session('parent', 0)['state'] == 'open'


def test_reusing_close_id_with_different_abandonment_is_rejected(world):
    store, _, _ = world
    row = store.open_session('parent', 0)
    close(store, 'parent', row)
    with pytest.raises(Conflict):
        store.close('parent', row['manifestRevision'], 'close-parent', abandoned=True)


def test_status_and_unchanged_observation_do_not_reset_stall_timers(world):
    store, _, now = world
    wave, row = register_accepted(store)
    close(store, 'parent', row)
    store.observe(wave, 'photo-1', 'revision-1', outcome='pending', evidence_id='stuck-lease', outstanding_leases=1)
    finish_scan(store, wave)
    now[0] += 86400
    before = store.status(wave)
    store.observe(wave, 'photo-1', 'revision-1', outcome='pending', evidence_id='stuck-lease', outstanding_leases=1)
    finish_scan(store, wave)
    after = store.status(wave)
    assert before['stateAgeSeconds'] == after['stateAgeSeconds'] == 86400
    assert before['noProgressSeconds'] == after['noProgressSeconds'] == 86400
    assert after['outstandingLeases'] == 1


def test_readiness_covers_members_across_pages_and_never_skips_unsettled_tail(world):
    store, _, _ = world
    parent = store.open_session('parent', 25)
    wave = parent['waveId']
    for start, stop in ((0, 20), (20, 25)):
        store.register('parent', [{'itemId': 'i' + str(i), 'filename': str(i) + '.jpg'}
                                  for i in range(start, stop)])
        for i in range(start, stop):
            parent = store.resolve('parent', 'i' + str(i), outcome='accepted',
                photo_id='photo-' + str(i), processing_revision='revision-1',
                filename=str(i) + '.jpg', required_steps=['face'])
            if i != 24:
                settle(store, wave, 'photo-' + str(i))
    close(store, 'parent', parent)
    assert finish_scan(store, wave)['outstandingMembers'] == 1
    assert store.status(wave)['state'] == 'processing'
    settle(store, wave, 'photo-24')
    result = finish_scan(store, wave)
    assert result['state'] == 'ready' and result['memberCount'] == 25


def test_retry_session_links_to_existing_parent_without_changing_its_manifest(world):
    store, _, _ = world
    original = store.open_session('original', 901)
    retry = store.open_session('retry', 1, resumed_from='original')
    assert retry['waveId'] == original['waveId']
    assert retry['resumedFromSessionId'] == 'original'
    assert store.open_session('retry', 1, resumed_from='original') == retry
    with pytest.raises(Conflict):
        store.open_session('retry', 1)
    assert store._required(store.session_key('original'))['declaredCount'] == 901


def test_retry_cannot_reference_an_unknown_or_cross_library_parent(world):
    store, table, _ = world
    store.open_session('original', 1)
    other = ShadowWaves(table, 'library-B')
    with pytest.raises(Conflict):
        other.open_session('retry', 1, resumed_from='original')
    with pytest.raises(Conflict):
        store.open_session('retry', 1, resumed_from='missing')


def test_conflict_retries_yield_before_rereading_without_weakening_bounds(world, monkeypatch):
    import upload_waves
    store, _, _ = world
    pauses, attempts = [], []
    monkeypatch.setattr(upload_waves.time, 'sleep', pauses.append)
    def competing_write():
        attempts.append(1)
        if len(attempts) < 4:
            raise ResourceModifiedError('another request committed')
        return 'committed'
    assert store._retry(competing_write) == 'committed'
    assert len(pauses) == 3 and all(0 < pause <= .05 for pause in pauses)


def test_contention_beyond_old_retry_budget_can_finish_without_partial_writes(world, monkeypatch):
    import upload_waves
    store, table, _ = world
    attempts, pauses = [], []
    original = table.submit_transaction
    monkeypatch.setattr(upload_waves.time, 'sleep', pauses.append)
    def competing(ops):
        attempts.append(1)
        if len(attempts) <= 10:
            raise ResourceModifiedError('busy library control row')
        return original(ops)
    monkeypatch.setattr(table, 'submit_transaction', competing)
    result = store.open_session('after-contention', 0)
    assert result['state'] == 'open'
    assert len(attempts) == 11  # ten conflicts, then the atomic intake commit
    assert len(pauses) == 10 and all(0 < pause <= .5 for pause in pauses)
    assert len(table.transactions) == 1  # the atomic intake commit


def test_storage_contention_is_distinct_and_records_failed_operation(world, monkeypatch):
    import processing_diagnostics
    import upload_waves
    from upload_waves import MutationContention
    store, _, _ = world
    events = []
    monkeypatch.setattr(upload_waves.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(processing_diagnostics, 'emit', lambda name, **fields: events.append((name, fields)))
    def conflict(): raise ResourceModifiedError('ETag changed')
    with pytest.raises(MutationContention) as caught:
        store._retry(conflict)
    assert isinstance(caught.value, Conflict)
    assert events[0][0] == 'wave_mutation_contention'
    assert events[0][1]['operation'].endswith('.conflict')
    assert events[0][1]['attempts'] == MAX_RETRIES
    assert isinstance(caught.value.__cause__, ResourceModifiedError)
