"""Batch efficiency, parallel ownership, and interrupted-page recovery."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from azure.core.exceptions import ResourceNotFoundError

from execution_lease import OwnershipLost, guard_operations
from upload_wave_execution import run_vision_release
from wave_source_batch import MetadataBatch, reserve_attempts
from tests.test_wave_execution_consumers import (
    world, FakeMetadata, settled_row, publish_completion as _publish_completion, no_faces,
)
from tests.test_upload_waves import close


def publish_completion(store, metadata, *, filename):
    metadata.rows[store.library, filename]['face_status'] = 'no_data'
    return _publish_completion(store, metadata, filename=filename)


class QueryMetadata(FakeMetadata):
    def __init__(self):
        super().__init__()
        self.queries = 0
        self.reads = 0

    def query_entities(self, query, results_per_page=20):
        assert len(re.findall(r'\b(?:PartitionKey|RowKey) eq ', query)) <= 15
        self.queries += 1
        library = re.search(r"PartitionKey eq '((?:[^']|'')*)'", query)[1].replace("''", "'")
        names = {name.replace("''", "'") for name in re.findall(r"RowKey eq '((?:[^']|'')*)'", query)}
        return [dict(row) for (owner, name), row in self.rows.items() if owner == library and name in names]

    def get_entity(self, *args):
        self.reads += 1
        return super().get_entity(*args)


def admit(world, count):
    store, work, leases, *_ = world
    metadata = QueryMetadata()
    session = store.open_session('batch', count)
    for offset in range(0, count, 20):
        store.register('batch', [dict(itemId=str(i), filename=f'photo-{i}.jpg')
                                for i in range(offset, min(count, offset + 20))])
    for i in range(count):
        filename = f'photo-{i}.jpg'
        session = store.resolve('batch', str(i), outcome='accepted', photo_id=f'photo-{i}',
            processing_revision='revision-1', filename=filename,
            required_steps=['ai_vision', 'exif', 'face', 'map_detection', 'ocr', 'preview', 'thumbnail'])
        metadata.put(store.library, filename,
            **dict(settled_row(f'photo-{i}', 'revision-1', filename=filename), face_status='pending', PartitionKey=store.library))
    close(store, 'batch', session)
    wave = session['waveId']
    return metadata, wave, store.status(wave)['visionPendingKey']


def run(world, metadata, wave, key, process, **kwargs):
    store, work, leases, *_ = world
    return run_vision_release(leases, work, store, store.library, wave, key,
        metadata, None, no_faces, lambda *args: None, process_source=process, **kwargs)


def test_parallel_sources_inherit_stage_scope_and_batch_observation_writes(world, monkeypatch):
    metadata, wave, key = admit(world, 8)
    store, _, leases, table, _ = world
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = [0]
    peak = [0]
    original = table.submit_transaction
    observation_batches = []
    attempt_batches = []
    def submit(operations):
        operations = list(operations)
        members = [row for _, row, *_ in operations
                   if row['RowKey'].startswith(f'e!{wave}!m!') and row.get('outcome') == 'settled']
        attempts = [row for _, row, *_ in operations if row['RowKey'].startswith('source-attempt!')]
        if members: observation_batches.append(len(members))
        if attempts: attempt_batches.append(len(attempts))
        return original(operations)
    monkeypatch.setattr(table, 'submit_transaction', submit)
    checkpoints = []
    checkpoint = leases.checkpoint
    monkeypatch.setattr(leases, 'checkpoint', lambda owner, value:
        (checkpoints.append(value), checkpoint(owner, value))[1])
    def process(library, filename, revision, renew):
        assert guard_operations(store), 'Execution context was lost in a source thread'
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            barrier.wait(timeout=5)
            renew()
            publish_completion(store, metadata, filename=filename)
        finally:
            with lock: active[0] -= 1
    result = run(world, metadata, wave, key, process, source_workers=2)
    assert result['state'] == 'ready'
    assert peak[0] == 2
    assert observation_batches == [8]
    assert sum(attempt_batches) == 8
    assert all(1 <= size <= 2 for size in attempt_batches)
    assert len([value for value in checkpoints if '"phase": "sources"' in value]) == 1
    assert metadata.queries == 2  # Selection and fresh post-source evidence planning.


def test_opt_in_image_batches_propagate_to_source_pool_without_write_guards(world, monkeypatch):
    import vision_utils
    from image_batching import current_batcher
    metadata, wave, key = admit(world, 2)
    store = world[0]
    barrier = threading.Barrier(2)
    seen = []
    def vector(name):
        return [float(i == int(name.split('-')[1].split('.')[0])) for i in range(512)]
    def encode(names):
        assert not guard_operations(store), 'Broker inherited a source write/execution context'
        seen.append(names)
        return [vector(name) for name in names]
    monkeypatch.setenv('IPWORK_CLIP_IMAGE_BATCH_SIZE', '2')
    monkeypatch.setenv('IPWORK_CLIP_IMAGE_BATCH_WINDOW_MS', '50')
    monkeypatch.setattr(vision_utils, '_encode_image_tensors_batch', encode)
    monkeypatch.setattr(vision_utils, '_encode_image_tensor_single', lambda _: pytest.fail('Lost pair'))
    def process(library, filename, revision, renew):
        assert guard_operations(store)
        barrier.wait(5)
        assert current_batcher().submit(filename) == vector(filename)
        renew()
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process, source_workers=2)['state'] == 'ready'
    assert len(seen) == 1
    assert current_batcher() is None


def test_repeated_parallel_checks_do_not_repeat_remote_stage_and_policy_reads(world, monkeypatch):
    import processing_checks
    import processing_diagnostics
    from processing_cleanup import check_source_policy
    metadata, wave, key = admit(world, 2)
    store, _, _, table, _ = world
    original_guard = processing_checks.CoalescedCheck
    monkeypatch.setattr(processing_checks, 'CoalescedCheck',
        lambda *args, **kwargs: original_guard(*args, clock=lambda: 0.0, **kwargs))
    reads = []
    original_read = table.get_entity
    def read(*args, **kwargs):
        reads.append(args)
        return original_read(*args, **kwargs)
    monkeypatch.setattr(table, 'get_entity', read)
    emitted = []
    original_emit = processing_diagnostics.emit
    def emit(event, **fields):
        if event in ('vision_policy_checks', 'vision_stage_checks'):
            emitted.append((event, fields))
        original_emit(event, **fields)
    monkeypatch.setattr(processing_diagnostics, 'emit', emit)
    def process(library, filename, revision, renew):
        for _ in range(1000):
            renew()
            check_source_policy(table, library)
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process, source_workers=2)['state'] == 'ready'
    assert len(reads) < 300  # Includes real admission, receipts and barrier writes.
    stats = dict(emitted)
    assert stats['vision_policy_checks']['cached_checks'] >= 2000
    assert stats['vision_stage_checks']['cached_checks'] >= 2000
    assert stats['vision_stage_checks']['forced_checks'] >= 2


def test_completed_page_uses_one_metadata_read_and_no_new_attempts(world):
    metadata, wave, key = admit(world, 20)
    store = world[0]
    for filename in [name for owner, name in metadata.rows]:
        publish_completion(store, metadata, filename=filename)
    run(world, metadata, wave, key, lambda *a: pytest.fail('Reran qualified model work'))
    assert metadata.queries == 2
    assert metadata.reads == 0
    assert not store._page('source-attempt!')


def test_source_failure_is_single_attempt_and_does_not_block_siblings(world):
    metadata, wave, key = admit(world, 2)
    store = world[0]
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        if filename == 'photo-0.jpg':
            raise RuntimeError('deterministic decode failure')
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process, source_workers=2)['state'] == 'ready'
    assert len(calls) == 2
    assert store.status(wave)['failedMembers'] == 1
    assert [row['count'] for row in store._page('source-attempt!')] == [1, 1]
    assert run(world, metadata, wave, key, process, source_workers=2) is None
    assert len(calls) == 2


def test_crash_before_batch_observation_replays_receipts_without_models(world, monkeypatch):
    import wave_source_batch
    metadata, wave, key = admit(world, 4)
    store = world[0]
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    commit = wave_source_batch.commit_observations
    monkeypatch.setattr(wave_source_batch, 'commit_observations',
        lambda *args: (_ for _ in ()).throw(RuntimeError('crash before batch commit')))
    with pytest.raises(RuntimeError, match='crash before batch'):
        run(world, metadata, wave, key, process, source_workers=2)
    assert len(calls) == 4
    monkeypatch.setattr(wave_source_batch, 'commit_observations', commit)
    result = run(world, metadata, wave, key, lambda *args: pytest.fail('Reran durable source work'), source_workers=2)
    assert result['state'] == 'ready'
    assert [row['count'] for row in store._page('source-attempt!')] == [1] * 4


def test_deadline_checkpoints_only_the_processed_prefix(world, monkeypatch):
    metadata, wave, key = admit(world, 4)
    store, _, leases, *_ = world
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
        clock[0] += 6
    run(world, metadata, wave, key, process, deadline=5)
    assert len(calls) == 1
    row = leases._get(leases.key(store.library, wave, 'vision'))
    position = json.loads(row['progressCheckpoint'][7:])
    first = store._page(f'e!{wave}!m!')[0]
    assert position == dict(phase='sources', after=first['RowKey'])
    for _ in range(4):
        run(world, metadata, wave, key, process, deadline=clock[0] + 5)
    assert len(calls) == len(set(calls)) == 4
    assert store.status(wave)['state'] == 'ready'


def test_owner_loss_drains_parallel_tasks_before_returning(world):
    metadata, wave, key = admit(world, 2)
    store, _, leases, *_ = world
    barrier = threading.Barrier(2)
    changed = threading.Event()
    finished = []
    lock = threading.Lock()
    counter = [0]
    def process(library, filename, revision, renew):
        with lock:
            index = counter[0]
            counter[0] += 1
        barrier.wait(timeout=5)
        if index == 0:
            row = leases._get(leases.key(store.library, wave, 'vision'))
            leases._replace(row, dict(row, owner='replacement-owner'))
            changed.set()
            renew()
        else:
            assert changed.wait(timeout=5)
            finished.append(filename)
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, process, source_workers=2)
    assert len(finished) == 1
    assert all(member['outcome'] == 'pending' for member in store._page(f'e!{wave}!m!'))
    assert leases._get(leases.key(store.library, wave, 'vision'))['owner'] == 'replacement-owner'


def test_reserved_attempt_is_not_reexecuted_after_interruption(world):
    metadata, wave, key = admit(world, 1)
    store = world[0]
    member = store._page(f'e!{wave}!m!')[0]
    assert reserve_attempts(store, [member])[member['RowKey']] == (1, True)
    result = run(world, metadata, wave, key, lambda *args: pytest.fail('Repeated admitted model attempt'))
    assert result['state'] == 'ready'
    assert store.status(wave)['failedMembers'] == 1
    assert store._page('source-attempt!')[0]['count'] == 1


def test_metadata_batch_escapes_names_and_invalidates_after_source_writes():
    metadata = QueryMetadata()
    metadata.put("owner's", "a'b.jpg", PartitionKey="owner's", RowKey="a'b.jpg", version=1)
    batch = MetadataBatch(metadata, "owner's", [dict(filename="a'b.jpg")])
    assert batch.get_entity("owner's", "a'b.jpg")['version'] == 1
    metadata.rows["owner's", "a'b.jpg"]['version'] = 2
    batch.invalidate("a'b.jpg")
    assert batch.get_entity("owner's", "a'b.jpg")['version'] == 2
    missing = MetadataBatch(metadata, "owner's", [dict(filename='missing.jpg')])
    with pytest.raises(ResourceNotFoundError):
        missing.get_entity("owner's", 'missing.jpg')


def test_batch_conflict_preserves_a_concurrent_terminal_settlement(world, monkeypatch):
    from wave_settlement import settle
    metadata, wave, key = admit(world, 4)
    store, _, _, table, _ = world
    original = table.submit_transaction
    injected = []
    def submit(operations):
        operations = list(operations)
        changes = [row for _, row, *_ in operations
                   if row['RowKey'].startswith(f'e!{wave}!m!') and row.get('outcome') == 'settled']
        if changes and not injected:
            injected.append(changes[0]['RowKey'])
            member = store._required(changes[0]['RowKey'])
            settle(store, wave, member, 'deleted', 'concurrent_source_deletion', witness=dict(deleted=True))
        return original(operations)
    monkeypatch.setattr(table, 'submit_transaction', submit)
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    result = run(world, metadata, wave, key, process, source_workers=2)
    assert result['state'] == 'ready'
    assert store._required(injected[0])['outcome'] == 'deleted'
    assert len([row for row in store._page(f'e!{wave}!m!') if row['outcome'] == 'settled']) == 3


def test_attempt_batch_conflict_does_not_reissue_the_existing_attempt(world):
    metadata, wave, key = admit(world, 2)
    store = world[0]
    members = store._page(f'e!{wave}!m!')
    assert reserve_attempts(store, members[:1])[members[0]['RowKey']] == (1, True)
    counts = reserve_attempts(store, members)
    assert counts[members[0]['RowKey']] == (1, False)
    assert counts[members[1]['RowKey']] == (1, True)
    assert [row['count'] for row in store._page('source-attempt!')] == [1, 1]


def test_receipt_reuse_does_not_skip_validation_or_cache_missing_artifacts(world, monkeypatch):
    from wave_source_batch import ReceiptBatch
    metadata, wave, key = admit(world, 1)
    store, _, _, table, _ = world
    intent = publish_completion(store, metadata, filename='photo-0.jpg')
    batch = ReceiptBatch(table, store.library)
    original = table.get_entity
    reads = []
    def get(partition, key):
        reads.append(key)
        return original(partition, key)
    monkeypatch.setattr(table, 'get_entity', get)
    batch.verify_reference(intent['artifactReceipt'], intent)
    batch.verify_reference(intent['artifactReceipt'], intent)
    assert len([key for key in reads if key.startswith('manifest!')]) == 1
    changed = dict(intent, statuses=dict(intent['statuses'], face='failed'))
    with pytest.raises(ValueError, match='does not cover'):
        batch.verify_reference(intent['artifactReceipt'], changed)
    assert batch._get('face!late') is None
    table.submit_transaction([('create', dict(PartitionKey=batch.partition, RowKey='face!late', payload='{}'))])
    assert batch._get('face!late') is not None


def test_receipts_survive_page_boundary_but_inspection_snapshots_are_rebuilt(world, monkeypatch):
    import wave_source_batch
    metadata, wave, key = admit(world, 21)
    store = world[0]
    original = wave_source_batch.InspectionBatch
    seen = []
    def inspect(store, members, metadata, receipts):
        if seen:
            assert receipts is seen[0][0]
            assert receipts._get('face!page-marker') == {'payload': 'immutable'}
        snapshot = original(store, members, metadata, receipts)
        receipts._remember('face!page-marker', {'payload': 'immutable'})
        seen.append((receipts, snapshot))
        return snapshot
    monkeypatch.setattr(wave_source_batch, 'InspectionBatch', inspect)
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    assert len(seen) == 4  # Selection and post-source snapshots for each page.
    assert seen[0][1] is not seen[1][1]
    assert set(seen[0][1].proofs).isdisjoint(seen[2][1].proofs)


def test_preparation_deadline_does_not_consume_model_attempts(world, monkeypatch):
    metadata, wave, key = admit(world, 2)
    store = world[0]
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    def prepare(*args):
        clock[0] = 10.0
    result = run(world, metadata, wave, key, lambda *args: pytest.fail('Started after deadline'),
                 prepare_sources=prepare, deadline=5)
    assert result['state'] == 'processing_pending'
    assert not store._page('source-attempt!')


def test_parallel_waves_share_the_same_bounded_source_slots(world):
    from execution_lease import ExecutionLeases
    from upload_waves import ShadowWaves
    store, work, _, table, work_table = world
    other = ShadowWaves(table, 'library-B')
    other_world = (other, work, ExecutionLeases(table, partition=other.partition), table, work_table)
    metadata_a, wave_a, key_a = admit(world, 4)
    metadata_b, wave_b, key_b = admit(other_world, 4)
    active = [0]
    peak = [0]
    lock = threading.Lock()
    sources = {store.library: (store, metadata_a), other.library: (other, metadata_b)}
    def process(library, filename, revision, renew):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.005)
            owner, metadata = sources[library]
            publish_completion(owner, metadata, filename=filename)
        finally:
            with lock: active[0] -= 1
    with ThreadPoolExecutor(max_workers=2) as source_pool, ThreadPoolExecutor(max_workers=2) as waves:
        futures = [waves.submit(run, w, metadata, wave, key, process,
                   source_workers=2, source_executor=source_pool)
                   for w, metadata, wave, key in ((world, metadata_a, wave_a, key_a),
                                                  (other_world, metadata_b, wave_b, key_b))]
        assert all(future.result()['state'] == 'ready' for future in futures)
    assert peak[0] == 2


def test_metadata_queries_bound_long_filename_filters():
    from urllib.parse import quote
    metadata = QueryMetadata()
    members = [dict(filename=str(index) + '-' + 'a' * 950 + '.jpg') for index in range(20)]
    for member in members:
        metadata.put('library-A', member['filename'], PartitionKey='library-A', RowKey=member['filename'])
    original = metadata.query_entities
    queries = []
    def query(filter_text, **kwargs):
        queries.append(filter_text)
        return original(filter_text, **kwargs)
    metadata.query_entities = query
    batch = MetadataBatch(metadata, 'library-A', members)
    assert len(queries) > 2
    assert all(len(quote(query)) <= 6000 for query in queries)
    assert all(batch.get_entity('library-A', member['filename'])['RowKey'] == member['filename'] for member in members)


def test_empty_serialized_outbox_requalifies_instead_of_skipping_member(world):
    metadata, wave, key = admit(world, 1)
    store = world[0]
    row = metadata.rows[store.library, 'photo-0.jpg']
    row.update(processingCompletionId='missing-completion', processingCompletionOutbox='[]')
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    assert calls == ['photo-0.jpg']
    assert store.status(wave)['failedMembers'] == 0
    assert not store._page(f'e!{wave}!m!')[0].get('inspectionFailures')


def test_inspection_failure_has_separate_bounded_budget_and_does_not_block_siblings(world, monkeypatch):
    import upload_wave_execution as execution
    from wave_settlement import MAX_INSPECTION_FAILURES, verified
    metadata, wave, key = admit(world, 2)
    store = world[0]
    inspect = execution._inspect_vision_member
    def broken(store, wave, member, *args):
        if member['filename'] == 'photo-0.jpg':
            raise IndexError('private details must not be persisted')
        return inspect(store, wave, member, *args)
    monkeypatch.setattr(execution, '_inspect_vision_member', broken)
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    for attempt in range(1, MAX_INSPECTION_FAILURES + 1):
        failures = [0]
        result = run(world, metadata, wave, key, process, inspection_failures=failures)
        bad = next(r for r in store._page(f'e!{wave}!m!') if r['filename'] == 'photo-0.jpg')
        assert bad['inspectionFailures'] == attempt
        assert bad['inspectionError'] == 'IndexError'
        assert 'private details' not in json.dumps(bad)
        assert failures == [int(attempt < MAX_INSPECTION_FAILURES)]
        assert result['state'] == ('ready' if attempt == MAX_INSPECTION_FAILURES else 'processing')
    assert bad['outcome'] == 'failed_exhausted' and verified(store, bad)
    proof = store._get('settlement!' + bad['evidenceId'])
    assert json.loads(proof['payload'])['reason'] == 'inspection_failures_exhausted'
    assert calls == ['photo-1.jpg']
    assert len(store._page('source-attempt!')) == 1
    assert store.status(wave)['failedMembers'] == 1


def test_transient_inspection_failure_can_recover_without_consuming_model_budget(world, monkeypatch):
    import upload_wave_execution as execution
    metadata, wave, key = admit(world, 1)
    original = execution._inspect_vision_member
    monkeypatch.setattr(execution, '_inspect_vision_member', lambda *a: (_ for _ in ()).throw(OSError('temporary')))
    run(world, metadata, wave, key, lambda *a: pytest.fail('Model started during failed inspection'))
    assert not world[0]._page('source-attempt!')
    monkeypatch.setattr(execution, '_inspect_vision_member', original)
    def process(library, filename, revision, renew):
        publish_completion(world[0], metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    assert world[0].status(wave)['failedMembers'] == 0


@pytest.mark.parametrize('blocked', ['missing', 'isolated', 'identity', 'contract'])
def test_silent_skip_has_bounded_failure_and_releases_healthy_siblings(world, blocked):
    from wave_settlement import BLOCKED_GRACE_SECONDS, verified
    metadata, wave, key = admit(world, 2)
    store = world[0]
    now = [store.clock()]
    store.clock = lambda: now[0]
    row = metadata.rows[store.library, 'photo-0.jpg']
    if blocked == 'missing': del metadata.rows[store.library, 'photo-0.jpg']
    elif blocked == 'isolated': row['processingIsolated'] = True
    elif blocked == 'identity': row['processingRevision'] = 'unadmitted'
    else: row['processingGeneration'] = 'obsolete'
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    for _ in range(3):
        assert run(world, metadata, wave, key, process)['state'] == 'processing'
    now[0] += BLOCKED_GRACE_SECONDS + 1
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    bad = next(m for m in store._page(f'e!{wave}!m!') if m['filename'] == 'photo-0.jpg')
    assert bad['outcome'] == 'failed_exhausted' and verified(store, bad)
    assert store.status(wave)['failedMembers'] == 1
    assert calls == ['photo-1.jpg']


def test_missing_source_recovers_during_grace(world):
    metadata, wave, key = admit(world, 1)
    row = metadata.rows.pop((world[0].library, 'photo-0.jpg'))
    run(world, metadata, wave, key, lambda *a: pytest.fail('missing source ran'))
    metadata.rows[world[0].library, 'photo-0.jpg'] = row
    def process(library, filename, revision, renew):
        publish_completion(world[0], metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    assert world[0].status(wave)['failedMembers'] == 0


def test_blocked_source_does_not_fail_a_live_owner(world):
    from datetime import datetime, timedelta, timezone
    from wave_settlement import blocked_source, BLOCKED_GRACE_SECONDS
    metadata, wave, _ = admit(world, 1)
    store = world[0]
    member, = store._page(f'e!{wave}!m!')
    row = metadata.rows[store.library, 'photo-0.jpg']
    row['processingIsolated'] = True
    now = [store.clock()]
    store.clock = lambda: now[0]
    for _ in range(3): assert not blocked_source(store, wave, member, metadata)
    now[0] += BLOCKED_GRACE_SECONDS + 1
    row.update(processing_lease_owner='other',
        processing_lease_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert not blocked_source(store, wave, member, metadata)
    assert store._required(member['RowKey'])['outcome'] == 'pending'
    assert not store._page('settlement!')


def test_blocked_source_rechecks_recovery_after_coordination_write(world, monkeypatch):
    from wave_settlement import blocked_source, BLOCKED_GRACE_SECONDS
    metadata, wave, _ = admit(world, 1)
    store = world[0]
    member, = store._page(f'e!{wave}!m!')
    row = metadata.rows[store.library, 'photo-0.jpg']
    row['processingIsolated'] = True
    now = [store.clock()]
    store.clock = lambda: now[0]
    for _ in range(3): assert not blocked_source(store, wave, member, metadata)
    now[0] += BLOCKED_GRACE_SECONDS + 1
    get = metadata.get_entity
    reads = [0]
    def recover(*args):
        reads[0] += 1
        if reads[0] == 4: row['processingIsolated'] = False
        return get(*args)
    monkeypatch.setattr(metadata, 'get_entity', recover)
    assert not blocked_source(store, wave, member, metadata)
    assert store._required(member['RowKey'])['outcome'] == 'pending'


def test_blocked_source_read_error_is_not_a_missing_source(world, monkeypatch):
    from wave_settlement import blocked_source
    metadata, wave, _ = admit(world, 1)
    store = world[0]
    member, = store._page(f'e!{wave}!m!')
    def fail(*args): raise OSError('storage unavailable')
    monkeypatch.setattr(metadata, 'get_entity', fail)
    with pytest.raises(OSError): blocked_source(store, wave, member, metadata)
    assert not store._required(member['RowKey']).get('blockedChecks')


def test_settled_member_losing_qualification_is_retracted_before_blocked_budget(world):
    metadata, wave, key = admit(world, 2)
    store = world[0]
    metadata.rows[store.library, 'photo-0.jpg']['processingIsolated'] = True
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'processing'
    metadata.rows.pop((store.library, 'photo-1.jpg'))
    assert run(world, metadata, wave, key, process)['state'] == 'processing'
    member = next(m for m in store._page(f'e!{wave}!m!') if m['filename'] == 'photo-1.jpg')
    assert member['outcome'] == 'pending'


def test_inspection_failure_cannot_fail_another_active_source_owner(world, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import upload_wave_execution as execution
    from wave_settlement import MAX_INSPECTION_FAILURES
    metadata, wave, key = admit(world, 1)
    row = metadata.rows[world[0].library, 'photo-0.jpg']
    row.update(processing_lease_owner='other',
               processing_lease_expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    monkeypatch.setattr(execution, '_inspect_vision_member', lambda *a: (_ for _ in ()).throw(ValueError('bad')))
    for _ in range(MAX_INSPECTION_FAILURES + 1):
        result = run(world, metadata, wave, key, lambda *a: pytest.fail('Stole active source'))
        assert result['state'] == 'processing'
    member, = world[0]._page(f'e!{wave}!m!')
    assert member['outcome'] == 'pending'
    assert member['inspectionFailures'] == MAX_INSPECTION_FAILURES
    assert not world[0]._page('settlement!')


def test_inspection_ownership_loss_is_not_swallowed_or_counted(world, monkeypatch):
    import upload_wave_execution as execution
    metadata, wave, key = admit(world, 1)
    monkeypatch.setattr(execution, '_inspect_vision_member', lambda *a: (_ for _ in ()).throw(OwnershipLost('lost')))
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, lambda *a: None)
    member, = world[0]._page(f'e!{wave}!m!')
    assert 'inspectionFailures' not in member


def test_frozen_face_inputs_use_bounded_queries_without_receipt_point_reads(world, monkeypatch):
    from upload_wave_execution import _iter_wave_faces
    metadata, wave, key = admit(world, 20)
    store, _, _, table, _ = world
    for i in range(20):
        filename = f'photo-{i}.jpg'
        metadata.rows[store.library, filename]['face_status'] = 'done'
        version = json.loads(metadata.rows[store.library, filename]['processingContract'])['faceEmbeddingVersions'][0]
        _publish_completion(store, metadata, filename=filename, faces=[dict(
            PartitionKey=store.library, RowKey=f'face-{i}', filename=filename,
            sourcePhotoId=f'photo-{i}', processingRevision='revision-1', embedding='[1,0]', embeddingVersion=version)])
    run(world, metadata, wave, key, lambda *a: pytest.fail('Reran completed image'))
    original = table.get_entity
    reads = []
    monkeypatch.setattr(table, 'get_entity', lambda *a, **kw: (reads.append(a), original(*a, **kw))[1])
    table.queries.clear()
    faces = list(_iter_wave_faces(store, wave))
    assert len(faces) == 20
    assert not reads
    assert len(table.queries) == 8  # Two queries per inbox/manifest/face set, two member pages.
    # Prefetch changes transport only; every snapshot still needs a valid hash.
    face_key = next(k for k in table.rows if k[1].startswith('face!'))
    table.rows[face_key]['payload'] = '{}'
    with pytest.raises(ValueError, match='invalid face snapshots'):
        list(_iter_wave_faces(store, wave))


def test_rolling_pool_refills_before_slow_sibling_finishes(world):
    """The third source must run while the first pair's slow source is live."""
    metadata, wave, key = admit(world, 4)
    store = world[0]
    names = [m['filename'] for m in store._page(f'e!{wave}!m!')]
    first_started, replacement_started = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, peak = [0], [0]
    def process(library, filename, revision, renew):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if filename == names[0]:
                first_started.set()
                assert replacement_started.wait(5), 'Pool waited for the whole pair'
            elif filename == names[1]:
                assert first_started.wait(5)
            elif filename == names[2]:
                assert active[0] == 2
                replacement_started.set()
            publish_completion(store, metadata, filename=filename)
        finally:
            with lock: active[0] -= 1
    assert run(world, metadata, wave, key, process, source_workers=2)['state'] == 'ready'
    assert replacement_started.is_set() and peak[0] == 2


def test_rolling_deadline_drains_admitted_sources_without_charging_tail(world, monkeypatch):
    metadata, wave, key = admit(world, 5)
    store, _, leases, *_ = world
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    both_started = threading.Barrier(2)
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        both_started.wait(5)
        publish_completion(store, metadata, filename=filename)
        clock[0] = 10
    run(world, metadata, wave, key, process, source_workers=2, deadline=5)
    assert len(calls) == 2
    assert len(store._page('source-attempt!')) == 2
    members = store._page(f'e!{wave}!m!')
    row = leases._get(leases.key(store.library, wave, 'vision'))
    assert json.loads(row['progressCheckpoint'][7:]) == dict(phase='sources', after=members[1]['RowKey'])
    assert [m['outcome'] for m in members] == ['settled', 'settled', 'pending', 'pending', 'pending']


def test_admission_exception_drains_shared_executor_before_releasing_stage(world, monkeypatch):
    import wave_source_batch
    metadata, wave, key = admit(world, 4)
    store = world[0]
    names = [m['filename'] for m in store._page(f'e!{wave}!m!')]
    slow_started, release_slow, slow_finished = (threading.Event() for _ in range(3))
    original = wave_source_batch.reserve_attempts
    admissions = []
    def reserve(store, members):
        admissions.append(len(members))
        if len(admissions) == 2:
            release_slow.set()
            raise RuntimeError('admission unavailable')
        return original(store, members)
    monkeypatch.setattr(wave_source_batch, 'reserve_attempts', reserve)
    def process(library, filename, revision, renew):
        if filename == names[0]:
            slow_started.set()
            assert release_slow.wait(5)
        else:
            assert slow_started.wait(5)
        publish_completion(store, metadata, filename=filename)
        if filename == names[0]: slow_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        with pytest.raises(RuntimeError, match='admission unavailable'):
            run(world, metadata, wave, key, process, source_workers=2, source_executor=pool)
        assert slow_finished.is_set(), 'Returned while a shared-executor source could still write'
    assert admissions == [2, 1]
    assert len(store._page('source-attempt!')) == 2


def test_inspection_prefetches_inboxes_and_receipts_without_member_point_reads(world, monkeypatch):
    metadata, wave, key = admit(world, 20)
    store, _, _, table, _ = world
    for _, filename in list(metadata.rows):
        publish_completion(store, metadata, filename=filename)
    original = table.get_entity
    reads = []
    def get(partition, key):
        reads.append(key)
        return original(partition, key)
    monkeypatch.setattr(table, 'get_entity', get)
    assert run(world, metadata, wave, key, lambda *a: pytest.fail('Reprocessed qualified source'))['state'] == 'ready'
    selection_prefixes = ('source-completion!', 'manifest!', 'face!', 'deleted-source!', 'supersession!')
    assert not [key for key in reads if key.startswith(selection_prefixes)]
    assert not store._page('source-attempt!')


def test_prefetched_receipt_still_rejects_corrupt_manifest(world):
    from wave_source_batch import InspectionBatch, ReceiptBatch, MetadataBatch
    from upload_wave_execution import _inspect_vision_member
    metadata, wave, _ = admit(world, 1)
    store, _, _, table, _ = world
    intent = publish_completion(store, metadata, filename='photo-0.jpg')
    partition = ReceiptBatch(table, store.library).partition
    manifest = table.rows[partition, 'manifest!' + intent['artifactReceipt']['id']]
    payload = json.loads(manifest['payload']); payload['faceCount'] = 123
    manifest['payload'] = json.dumps(payload)
    members = store._page(f'e!{wave}!m!')
    rows = MetadataBatch(metadata, store.library, members)
    receipts = ReceiptBatch(table, store.library)
    receipts.inspection = InspectionBatch(store, members, rows, receipts)
    with pytest.raises(ValueError, match='does not cover'):
        _inspect_vision_member(store, wave, members[0], rows, receipts)


def test_completed_sources_use_separate_proof_families_without_point_reads(world, monkeypatch):
    from wave_source_batch import InspectionBatch, ReceiptBatch, MetadataBatch
    from upload_wave_execution import _inspect_vision_member
    metadata, wave, _ = admit(world, 20)
    store, _, _, table, _ = world
    for _, filename in list(metadata.rows):
        publish_completion(store, metadata, filename=filename)
    members = store._page(f'e!{wave}!m!')
    metadata = MetadataBatch(metadata, store.library, members)
    original = table.get_entity
    reads = []
    monkeypatch.setattr(table, 'get_entity', lambda *a: (reads.append(a), original(*a))[1])
    baseline = ReceiptBatch(table, store.library)
    before = [_inspect_vision_member(store, wave, member, metadata, baseline) for member in members]
    assert len(reads) == 80
    reads.clear(); queries_before = len(table.queries)
    batched = ReceiptBatch(table, store.library)
    batched.inspection = InspectionBatch(store, members, metadata, batched)
    after = [_inspect_vision_member(store, wave, member, metadata, batched) for member in members]
    assert before == after
    assert reads == [] and len(table.queries) - queries_before == 8


def test_deletion_arriving_after_prefetch_wins_before_source_settlement(world):
    from wave_settlement import deletion_key
    metadata, wave, key = admit(world, 1)
    store, _, _, table, _ = world
    member = store._page(f'e!{wave}!m!')[0]
    def deleted_during_source(*args):
        table.submit_transaction([('create', store._row(
            deletion_key(member['photoId'], member['processingRevision']), deleted=True))])
    result = run(world, metadata, wave, key, deleted_during_source)
    assert result['state'] in ('ready', 'indexing', 'complete')
    assert store._required(member['RowKey'])['outcome'] == 'deleted'


@pytest.mark.parametrize('failure', ['metadata', 'evidence'])
def test_post_source_prefetch_failure_falls_back_without_reprocessing(world, monkeypatch, failure):
    import wave_source_batch as batches
    import processing_metrics as metrics
    metadata, wave, key = admit(world, 3)
    store = world[0]
    processed = []
    original = (batches.MetadataBatch if failure == 'metadata' else batches.InspectionBatch).__init__
    target = batches.MetadataBatch if failure == 'metadata' else batches.InspectionBatch
    failures = []
    def init(self, *args, **kwargs):
        if len(processed) == 3:
            failures.append(True)
            raise RuntimeError('Transient batch read failure')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(target, '__init__', init)
    def process(library, filename, revision, renew):
        processed.append(filename)
        publish_completion(store, metadata, filename=filename)
    with metrics.accounting() as counters:
        assert run(world, metadata, wave, key, process)['state'] == 'ready'
    values = counters.snapshot()
    assert values['vision_qualification_prefetch_calls'] == 1
    assert values['vision_qualification_prefetch_errors'] == 1
    assert values['vision_qualification_fallback_members'] == 3
    assert values['vision_qualification_fresh_metadata_reads'] == 3
    assert values['vision_qualification_completed'] == 3
    assert values['vision_qualification_verify_ms'] > 0
    assert failures and len(processed) == 3
    assert all(m['outcome'] == 'settled' for m in store._page(f'e!{wave}!m!'))


@pytest.mark.parametrize('change', ['replacement', 'lease'])
def test_post_source_snapshot_cannot_qualify_changed_metadata(world, monkeypatch, change):
    import wave_source_batch as batches
    metadata, wave, key = admit(world, 1)
    store = world[0]
    processed = []
    original = batches.InspectionBatch.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if processed:
            row = metadata.rows[store.library, 'photo-0.jpg']
            if change == 'replacement':
                row['processingRevision'] = 'replacement'
            else:
                row['processing_lease_owner'] = 'new-owner'
                row['processing_lease'] = 'new-lease'
                row['processing_lease_expires_at'] = '2999-01-01T00:00:00Z'
    monkeypatch.setattr(batches.InspectionBatch, '__init__', init)
    def process(library, filename, revision, renew):
        processed.append(filename)
        publish_completion(store, metadata, filename=filename)
    result = run(world, metadata, wave, key, process)
    assert result['state'] != 'ready'
    assert store._page(f'e!{wave}!m!')[0]['outcome'] == 'pending'


def test_post_source_qualification_batches_new_completion_evidence(world, monkeypatch):
    import upload_wave_execution as execution
    metadata, wave, key = admit(world, 20)
    store, _, _, table, _ = world
    original_observe, original_get = execution._observe_qualified, table.get_entity
    qualification_reads = []
    active = [False]
    def get(partition, row_key):
        if active[0]:
            qualification_reads.append(row_key)
        return original_get(partition, row_key)
    def observe(*args, **kwargs):
        active[0] = True
        try:
            return original_observe(*args, **kwargs)
        finally:
            active[0] = False
    monkeypatch.setattr(table, 'get_entity', get)
    monkeypatch.setattr(execution, '_observe_qualified', observe)
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    assert run(world, metadata, wave, key, process)['state'] == 'ready'
    assert not [key for key in qualification_reads
                if key.startswith(('source-completion!', 'manifest!', 'face!'))]
