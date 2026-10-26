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

    def query_entities(self, query, results_per_page):
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
    assert attempt_batches == [2, 2, 2, 2]
    assert len([value for value in checkpoints if '"phase": "sources"' in value]) == 1
    assert metadata.queries == 1


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
        metadata.put('library-A', member['filename'], RowKey=member['filename'])
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
