"""Opt-in upload hash merges: streaming, authoritative reads and real CAS semantics."""
import copy
import gzip
import hashlib
import io
import json
import tracemalloc

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableEntity, UpdateMode

import index_files
from index_work import DirtySnapshot
import storage_utils as su
import upload_hash_snapshot as snapshots


def file_hash(number):
    return hashlib.sha256(str(number).encode()).hexdigest()


class HashTable:
    """Every mutation changes an ETag; transactions validate before any writes."""

    def __init__(self):
        self.rows = {}
        self.revision = 0
        self.gets = []
        self.scans = 0
        self.failures = {}
        self.before_transaction = None
        self.transactions = []

    def get_entity(self, partition_key, row_key):
        self.gets.append((partition_key, row_key))
        if row_key in self.failures:
            raise self.failures[row_key]
        try:
            return copy.deepcopy(self.rows[partition_key, row_key])
        except KeyError:
            raise ResourceNotFoundError('missing')

    def upsert_entity(self, entity):
        self.revision += 1
        row = TableEntity(dict(entity))
        # The SDK deserializer sets an instance _metadata; TableEntity's bare
        # constructor otherwise leaves a shared class-level metadata default.
        row._metadata = {'etag': f'"revision-{self.revision}"'}
        self.rows[row['PartitionKey'], row['RowKey']] = row

    def create_entity(self, entity):
        if (entity['PartitionKey'], entity['RowKey']) in self.rows:
            raise ResourceExistsError('exists')
        self.upsert_entity(entity)

    def delete_entity(self, partition_key, row_key):
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError('missing')
        del self.rows[partition_key, row_key]

    def query_entities(self, query, **kwargs):
        self.scans += 1
        user_id = query.removeprefix("PartitionKey eq '").removesuffix("'").replace("''", "'")
        for (partition, _), row in self.rows.items():
            if partition == user_id:
                yield dict(row)

    def submit_transaction(self, operations):
        if self.before_transaction:
            hook, self.before_transaction = self.before_transaction, None
            hook()
        self.transactions.append(operations)
        for operation in operations:
            verb, entity = operation[:2]
            key = entity['PartitionKey'], entity['RowKey']
            if verb == 'update':
                options = operation[2]
                assert options['mode'] == UpdateMode.REPLACE
                assert options['match_condition'] == MatchConditions.IfNotModified
                if key not in self.rows or self.rows[key].metadata.get('etag') != options['etag']:
                    raise ResourceModifiedError('ETag mismatch')
            elif verb == 'delete' and key not in self.rows:
                raise ResourceNotFoundError('missing')
            elif verb == 'create' and key in self.rows:
                raise ResourceExistsError('exists')
        for operation in operations:
            verb, entity = operation[:2]
            if verb == 'delete':
                self.delete_entity(entity['PartitionKey'], entity['RowKey'])
            else:
                self.upsert_entity(entity)


class Downloader:
    def __init__(self, data, world):
        self.data, self.world = data, world

    def readinto(self, target):
        self.world.streamed += 1
        for start in range(0, len(self.data), 8192):
            target.write(memoryview(self.data)[start:start + 8192])

    def readall(self):
        if self.world.forbid_readall:
            pytest.fail('incremental builder downloaded the entire blob into RAM')
        return self.data


class Blob:
    def __init__(self, world, key):
        self.world, self.key = world, key

    def download_blob(self, **kwargs):
        assert kwargs.get('decompress') is False
        return Downloader(self.world.blobs[self.key], self.world)

    def upload_blob(self, data, overwrite, content_settings):
        if not overwrite and self.key in self.world.blobs:
            raise ResourceExistsError('immutable blob exists')
        assert content_settings.content_type == 'application/json'
        assert content_settings.content_encoding == 'gzip'
        self.world.blobs[self.key] = data.read()
        self.world.overwrites.append(overwrite)
        if self.world.after_upload:
            hook, self.world.after_upload = self.world.after_upload, None
            hook()

    def delete_blob(self):
        self.world.blobs.pop(self.key, None)


class World:
    def __init__(self):
        self.table = HashTable()
        self.blobs = {}
        self.overwrites = []
        self.streamed = 0
        self.forbid_readall = False
        self.after_upload = None

    def put(self, number, filename=None, user='lib'):
        self.table.upsert_entity({'PartitionKey': user, 'RowKey': file_hash(number),
                                 'filename': filename or f'{number}.jpg'})

    def payload(self, manifest):
        return json.loads(gzip.decompress(self.blobs[manifest['blobName']]))

    def upgrade(self, base, hashes, **kwargs):
        self.forbid_readall = True
        try:
            return snapshots.upgrade_snapshot('lib', hashes, expected_blob_name=base['blobName'], **kwargs)
        finally:
            self.forbid_readall = False


@pytest.fixture
def world(monkeypatch, tmp_path):
    world = World()
    monkeypatch.setitem(su._CTX, 'hash_index_table_client', world.table)
    monkeypatch.setattr(su, '_get_blob_client', lambda container, name: Blob(world, name))
    monkeypatch.setattr(index_files, 'WORK_DIR', str(tmp_path))
    # Exercise streaming writes even though this fake isn't a native SDK class.
    monkeypatch.setattr(su, '_upload_file_to_blob', lambda blob, path, **kw: _upload(blob, path, **kw))
    world.put(1, 'original.jpg')
    world.put(2, 'delete.jpg')
    world.put(3, 'unchanged.jpg')
    world.put(99, 'private.jpg', user='other')
    return world


def _upload(blob, path, **kwargs):
    with open(path, 'rb') as source:
        blob.upload_blob(source, **kwargs)


def test_add_delete_update_equivalent_to_full_rebuild_and_reader_contract(world):
    base = snapshots.rebuild_snapshot('lib')
    world.put(1, 'renamed-雪-"\\.jpg')
    world.put(4, 'added.jpg')
    snapshots.delete_hash(world.table, 'lib', file_hash(2))
    world.table.gets.clear()
    epoch = world.table.rows['lib', snapshots.DELETE_EPOCH_ROW]['epoch']
    merged = world.upgrade(base, [file_hash(1), file_hash(2), file_hash(4)],
                           complete_dirty_hashes=True, expected_delete_epoch=epoch)
    assert world.table.scans == 1
    assert {key for _, key in world.table.gets if key not in snapshots.CONTROL_ROWS} == {
        file_hash(1), file_hash(2), file_hash(4)}
    assert all(user == 'lib' for user, _ in world.table.gets)
    expected = {'hashes': {file_hash(1): 'renamed-雪-"\\.jpg', file_hash(3): 'unchanged.jpg',
                           file_hash(4): 'added.jpg'}}
    assert world.payload(merged) == expected
    assert world.overwrites[-1] is False and base['blobName'] in world.blobs
    payload, manifest, refresh = snapshots.read_snapshot('lib')
    assert json.loads(gzip.decompress(payload)) == expected
    assert manifest == merged and not refresh
    assert merged['compressedBytes'] == len(world.blobs[merged['blobName']])
    assert merged['sourceReadStartedAt'] == base['sourceReadStartedAt']
    assert world.payload(snapshots.rebuild_snapshot('lib')) == expected


def test_source_version_alias_and_no_changes_preserve_full_contract(world):
    base = snapshots.rebuild_snapshot('lib')
    result = snapshots.upgrade_snapshot('lib', [], previous_source_version=base['blobName'])
    assert world.payload(result) == world.payload(base)
    assert result['rows'] == base['rows'] and world.table.scans == 1


@pytest.mark.parametrize('kwargs', [{}, {'expected_blob_name': ''},
                                  {'expected_blob_name': 'x', 'previous_source_version': 'y'}])
def test_expected_base_is_required_and_aliases_must_agree(world, kwargs):
    with pytest.raises(ValueError, match='expected_blob_name'):
        snapshots.upgrade_snapshot('lib', [], **kwargs)
    assert world.table.gets == []


def test_stale_expected_base_defers_without_reads_or_fallback(world):
    base = snapshots.rebuild_snapshot('lib')
    current = snapshots.rebuild_snapshot('lib')
    with pytest.raises(RuntimeError, match='stale'):
        world.upgrade(base, [file_hash(1)])
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW]['blobName'] == current['blobName']
    assert world.streamed == 0 and world.table.scans == 2


@pytest.mark.parametrize('field,value', [('schemaVersion', 2), ('PartitionKey', 'other'),
                                      ('RowKey', 'wrong'), ('blobName', 'foreign.json.gz'),
                                      ('deleteEpoch', None), ('rows', -1)])
def test_invalid_manifest_defers(world, field, value):
    base = snapshots.rebuild_snapshot('lib')
    world.table.rows['lib', snapshots.SNAPSHOT_ROW][field] = value
    with pytest.raises(RuntimeError, match='invalid|stale'):
        world.upgrade(base, [])
    assert world.streamed == 0


def test_delete_epoch_change_requires_explicit_complete_changes_certification(world):
    base = snapshots.rebuild_snapshot('lib')
    snapshots.delete_hash(world.table, 'lib', file_hash(2))
    with pytest.raises(RuntimeError, match='certification'):
        world.upgrade(base, [file_hash(2)])
    assert snapshots.read_snapshot('lib')[0] is None
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW]['blobName'] == base['blobName']
    epoch = world.table.rows['lib', snapshots.DELETE_EPOCH_ROW]['epoch']
    with pytest.raises(RuntimeError, match='certification'):
        world.upgrade(base, [file_hash(2)], complete_dirty_hashes=True)
    merged = world.upgrade(base, [file_hash(2)], complete_dirty_hashes=True, expected_delete_epoch=epoch)
    assert file_hash(2) not in world.payload(merged)['hashes']
    assert merged['deleteEpoch'] == world.table.rows['lib', snapshots.DELETE_EPOCH_ROW]['epoch']


def test_only_confirmed_missing_removes_even_without_epoch_change(world):
    base = snapshots.rebuild_snapshot('lib')
    world.table.delete_entity('lib', file_hash(2))
    merged = world.upgrade(base, [file_hash(2), file_hash(100)])
    assert merged['rows'] == 2 and file_hash(2) not in world.payload(merged)['hashes']


@pytest.mark.parametrize('complete', [False, True])
@pytest.mark.parametrize('matches', [False, True])
def test_expected_delete_epoch_fences_even_without_epoch_change(world, complete, matches):
    base = snapshots.rebuild_snapshot('lib')
    assert base['deleteEpoch'] == ''
    kwargs = {'complete_dirty_hashes': complete,
              'expected_delete_epoch': base['deleteEpoch'] if matches else 'stale'}
    if matches:
        merged = world.upgrade(base, [], **kwargs)
        assert merged['deleteEpoch'] == ''
        assert world.payload(merged) == world.payload(base)
    else:
        with pytest.raises(RuntimeError, match='certified delete epoch changed'):
            world.upgrade(base, [], **kwargs)
        assert world.streamed == 0 and not world.table.transactions
        assert world.table.rows['lib', snapshots.SNAPSHOT_ROW] == base
        assert list(world.blobs) == [base['blobName']]


def test_transient_failures_retain_base_and_do_not_invent_new_hashes(world):
    base = snapshots.rebuild_snapshot('lib')
    world.put(1, 'not-read.jpg')
    world.put(4, 'not-read-new.jpg')
    world.table.failures.update({file_hash(1): RuntimeError('timeout'), file_hash(4): RuntimeError('503')})
    merged = world.upgrade(base, [file_hash(1), file_hash(4)])
    assert world.payload(merged) == world.payload(base)
    assert merged['sourceReadStartedAt'] == base['sourceReadStartedAt']


def test_transient_failure_across_delete_epoch_cannot_revalidate_deleted_base(world):
    base = snapshots.rebuild_snapshot('lib')
    snapshots.delete_hash(world.table, 'lib', file_hash(2))
    epoch = world.table.rows['lib', snapshots.DELETE_EPOCH_ROW]['epoch']
    world.table.failures[file_hash(2)] = RuntimeError('timeout, not confirmed missing')
    with pytest.raises(RuntimeError, match='unresolved'):
        world.upgrade(base, [file_hash(2)], complete_dirty_hashes=True, expected_delete_epoch=epoch)
    assert snapshots.read_snapshot('lib')[0] is None
    assert list(world.blobs) == [base['blobName']]


def test_deletion_before_initial_epoch_capture_cannot_use_outdated_certification(world, monkeypatch):
    base = snapshots.rebuild_snapshot('lib')
    snapshots.delete_hash(world.table, 'lib', file_hash(2))
    certified_epoch = world.table.rows['lib', snapshots.DELETE_EPOCH_ROW]['epoch']
    original = world.table.get_entity
    raced = False

    def get(partition_key, row_key):
        nonlocal raced
        if row_key == snapshots.DELETE_EPOCH_ROW and not raced:
            raced = True
            snapshots.delete_hash(world.table, 'lib', file_hash(3))
        return original(partition_key, row_key)

    monkeypatch.setattr(world.table, 'get_entity', get)
    with pytest.raises(RuntimeError, match='certified delete epoch changed'):
        world.upgrade(base, [file_hash(2)], complete_dirty_hashes=True,
                      expected_delete_epoch=certified_epoch)
    assert snapshots.read_snapshot('lib')[0] is None
    assert list(world.blobs) == [base['blobName']]


@pytest.mark.parametrize('race', ['after-upload', 'inside-transaction', 'after-source-read'])
def test_deletion_racing_publication_is_atomic_and_retains_candidate_for_gc(world, monkeypatch, race):
    base = snapshots.rebuild_snapshot('lib')
    delete = lambda: snapshots.delete_hash(world.table, 'lib', file_hash(1))
    if race == 'after-upload':
        world.after_upload = delete
    elif race == 'inside-transaction':
        world.table.before_transaction = delete
    else:
        original = world.table.get_entity

        def get(partition_key, row_key):
            row = original(partition_key, row_key)
            if row_key == file_hash(1):
                delete()
            return row

        monkeypatch.setattr(world.table, 'get_entity', get)
    with pytest.raises(ResourceModifiedError):
        world.upgrade(base, [file_hash(1)])
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW]['blobName'] == base['blobName']
    assert base['blobName'] in world.blobs and len(world.blobs) == 2
    assert snapshots.read_snapshot('lib')[0] is None


def test_competing_manifest_publication_cannot_be_overwritten(world):
    base = snapshots.rebuild_snapshot('lib')
    competitor = dict(base, updatedAt='competitor')
    world.table.before_transaction = lambda: world.table.upsert_entity(competitor)
    with pytest.raises(ResourceModifiedError):
        world.upgrade(base, [])
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW]['updatedAt'] == 'competitor'
    assert base['blobName'] in world.blobs and len(world.blobs) == 2


def test_callback_checks_before_source_reads_and_immediately_before_transaction(world):
    base = snapshots.rebuild_snapshot('lib')
    calls = []

    def check():
        calls.append(world.streamed)
        if len(calls) == 2:
            raise RuntimeError('lease lost')

    with pytest.raises(RuntimeError, match='lease lost'):
        world.upgrade(base, [file_hash(1)], publication_check=check)
    assert calls == [0, 1] and not world.table.transactions
    assert base['blobName'] in world.blobs and len(world.blobs) == 2


@pytest.mark.parametrize('row_key', [snapshots.SNAPSHOT_ROW, snapshots.DELETE_EPOCH_ROW])
@pytest.mark.parametrize('etag', [None, '*'])
def test_no_silent_no_etag_or_wildcard_fallback(world, row_key, etag):
    world.table.create_entity({'PartitionKey': 'lib', 'RowKey': snapshots.DELETE_EPOCH_ROW, 'epoch': 'e1'})
    base = snapshots.rebuild_snapshot('lib')
    world.table.rows['lib', row_key].metadata['etag'] = etag
    with pytest.raises(RuntimeError, match='native ETags'):
        world.upgrade(base, [])
    assert not world.table.transactions and world.streamed == 0


def test_transaction_support_required_even_without_publication_callback(world, monkeypatch):
    base = snapshots.rebuild_snapshot('lib')
    monkeypatch.setattr(world.table, 'submit_transaction', None)
    with pytest.raises(RuntimeError, match='transactional'):
        world.upgrade(base, [])


def test_epoch_etag_changes_even_if_epoch_value_is_unchanged_reject_publication(world):
    base = snapshots.rebuild_snapshot('lib')

    def touch():
        world.table.upsert_entity(world.table.rows['lib', snapshots.DELETE_EPOCH_ROW])

    world.table.before_transaction = touch
    with pytest.raises(ResourceModifiedError):
        world.upgrade(base, [])
    assert base['blobName'] in world.blobs and len(world.blobs) == 2


def test_512_targeted_reads_and_bounded_iterable_consumption(world):
    base = snapshots.rebuild_snapshot('lib')
    hashes = [file_hash(i) for i in range(512)]
    world.table.gets.clear()
    merged = world.upgrade(base, hashes)
    assert merged['rows'] == 3
    assert len([key for _, key in world.table.gets if key not in snapshots.CONTROL_ROWS]) == 512
    consumed = []

    def infinite():
        while True:
            consumed.append(1)
            yield file_hash(1)

    with pytest.raises(ValueError, match='512'):
        world.upgrade(merged, infinite())
    assert len(consumed) == 513 and world.table.scans == 1


@pytest.mark.parametrize('hashes', [None, 'a' * 64, ['A' * 64], [snapshots.SNAPSHOT_ROW], [17]])
def test_invalid_dirty_input_rejected(world, hashes):
    base = snapshots.rebuild_snapshot('lib')
    with pytest.raises(ValueError):
        world.upgrade(base, hashes)


@pytest.mark.parametrize('capture_kind', ['dirty-snapshot', 'has_more', 'truncated'])
@pytest.mark.parametrize('complete', [False, True])
def test_incomplete_dirty_capture_rejected_before_consumption(world, capture_kind, complete):
    base = snapshots.rebuild_snapshot('lib')
    if capture_kind == 'dirty-snapshot':
        hashes = DirtySnapshot([{'RowKey': file_hash(1)}], truncated=True)
    else:
        # No iteration protocol: checking completeness after enumeration would
        # raise TypeError instead of deliberately rejecting the capture.
        hashes = type('IncompleteCapture', (), {capture_kind: True})()
    world.table.gets.clear()
    with pytest.raises(ValueError, match='capture is incomplete'):
        world.upgrade(base, hashes, complete_dirty_hashes=complete,
                      expected_delete_epoch=base['deleteEpoch'],
                      publication_check=lambda: pytest.fail('invalid capture reached publication'))
    assert world.table.gets == [] and world.table.scans == 1
    assert world.streamed == 0 and not world.table.transactions
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW] == base
    assert list(world.blobs) == [base['blobName']]


@pytest.mark.parametrize('filename', ['x' * (64 * 1024 + 1), '雪' * (32 * 1024)])
def test_oversized_authoritative_row_defers_without_losing_base(world, filename):
    base = snapshots.rebuild_snapshot('lib')
    world.put(1, filename)
    with pytest.raises(RuntimeError, match='oversized'):
        world.upgrade(base, [file_hash(1)])
    assert list(world.blobs) == [base['blobName']] and world.streamed == 0


@pytest.mark.parametrize('field,value', [('filename', ''), ('filename', None),
                                      ('PartitionKey', 'other'), ('RowKey', file_hash(99))])
def test_invalid_authoritative_row_is_not_confirmed_missing(world, field, value):
    base = snapshots.rebuild_snapshot('lib')
    world.table.rows['lib', file_hash(1)][field] = value
    with pytest.raises(RuntimeError, match='authoritative'):
        world.upgrade(base, [file_hash(1)])
    assert world.payload(base)['hashes'][file_hash(1)] == 'original.jpg'


@pytest.mark.parametrize('data', [b'{"other":{}}', b'{"hashes":[]}', b'{"hashes":{',
                               b'{"hashes":{}} trailing', b'{"hashes":{},"extra":1}',
                               b'{"hashes":{"invalid":"x"}}',
                               ('{"hashes":{"' + file_hash(1) + '":3}}').encode(),
                               ('{"hashes":{"' + file_hash(1) + '":"\\ud800"}}').encode()])
def test_malformed_base_defers_without_full_parse_fallback(world, data):
    base = snapshots.rebuild_snapshot('lib')
    world.blobs[base['blobName']] = gzip.compress(data)
    with pytest.raises(RuntimeError):
        world.upgrade(base, [])
    assert list(world.blobs) == [base['blobName']] and world.table.scans == 1


def test_base_count_mismatch_and_truncated_gzip_defer(world):
    base = snapshots.rebuild_snapshot('lib')
    original = world.blobs[base['blobName']]
    world.table.rows['lib', snapshots.SNAPSHOT_ROW]['rows'] += 1
    with pytest.raises(RuntimeError, match='row count'):
        world.upgrade(base, [])
    world.table.rows['lib', snapshots.SNAPSHOT_ROW]['rows'] -= 1
    world.blobs[base['blobName']] = original[:-5]
    with pytest.raises(EOFError):
        world.upgrade(base, [])
    assert list(world.blobs) == [base['blobName']]


def test_parser_accepts_whitespace_escape_and_empty_object():
    data = (' { "hashes" : { "' + file_hash(1) + '" : "雪\\n\\\"\\\\" } } \n').encode()
    assert list(snapshots._SnapshotJSON(io.BytesIO(data)).hashes()) == [(file_hash(1), '雪\n"\\')]
    assert list(snapshots._SnapshotJSON(io.BytesIO(b'{"hashes":{}}')).hashes()) == []


def test_oversized_legacy_string_stops_at_bounded_token_not_entire_row(monkeypatch):
    monkeypatch.setattr(snapshots, 'INCREMENTAL_MAX_FILENAME_BYTES', 128)
    source = io.BytesIO((' {"hashes":{"' + file_hash(1) + '":"' + 'x' * 1_000_000 + '"}}').encode())
    with pytest.raises(RuntimeError, match='oversized'):
        list(snapshots._SnapshotJSON(source).hashes())
    assert source.tell() == snapshots._JSON_CHUNK_BYTES


def test_large_legacy_single_line_merge_does_not_materialize_map(world):
    for number in range(4, 12000):
        world.put(number)
    base = snapshots.rebuild_snapshot('lib')
    world.put(1, 'new.jpg')
    tracemalloc.start()
    try:
        merged = world.upgrade(base, [file_hash(1)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Includes the in-memory fake's compressed upload, gzip buffers, parser and
    # workspace, but not a 12k-entry Python map (~2MB even before filename data).
    assert peak < 2 * 1024 * 1024
    assert merged['rows'] == base['rows'] and world.table.scans == world.streamed == 1
    assert world.payload(merged)['hashes'][file_hash(1)] == 'new.jpg'


def test_blob_upload_failure_keeps_pointer_and_retains_partial_blob_for_gc(world):
    base = snapshots.rebuild_snapshot('lib')

    def fail():
        raise RuntimeError('upload failed')

    world.after_upload = fail
    with pytest.raises(RuntimeError, match='upload failed'):
        world.upgrade(base, [])
    assert base['blobName'] in world.blobs and len(world.blobs) == 2
    assert world.table.rows['lib', snapshots.SNAPSHOT_ROW]['blobName'] == base['blobName']