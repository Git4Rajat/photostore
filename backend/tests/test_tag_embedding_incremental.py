"""Explicit tag contributions: exact deletions, bounded merges, atomic provenance."""
import io
import json
import sqlite3

import numpy as np
import pytest
from azure.core.exceptions import ResourceNotFoundError

import storage_utils as storage
import tag_embedding_incremental as incremental
import vision_utils
import index_progress
from tests.fakes import FakeTable
from tests.test_people_embedding_index import _FakeBlobServiceClient, _FakeBlob, _Downloaded


@pytest.fixture
def ctx(monkeypatch):
    table, blobs, encoded = FakeTable(), _FakeBlobServiceClient(), []
    monkeypatch.setitem(storage._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage._CTX, 'blob_service_client', blobs)
    monkeypatch.setitem(storage._CTX, 'blob_tag_embedding_index_container', 'tags')
    monkeypatch.setattr(storage, 'DURABLE_INDEX_UPDATES', False)
    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'clip-test')
    def encode(tags):
        encoded.append(list(tags))
        return [[1.0, float(sum(map(ord, tag)) % 7 + 1)] for tag in tags]
    monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', encode)
    storage._TAG_EMBEDDING_INDEX_CACHE.clear()
    yield table, blobs, encoded
    storage._TAG_EMBEDDING_INDEX_CACHE.clear()


def seed(table, filename, tags, **kwargs):
    table.upsert_entity(dict(PartitionKey='u', RowKey=filename, tags=json.dumps(tags), **kwargs))


def bootstrap():
    return storage.refresh_user_tag_embedding_index('u', source_version='v1', model_version='clip-test',
                                                     bootstrap_incremental=True)


def refresh(ids, **kwargs):
    options = dict(source_version='v2', previous_source_version='v1', model_version='clip-test',
                   affected_filenames=ids, incremental_only=True)
    options.update(kwargs)
    return storage.refresh_user_tag_embedding_index('u', **options)


def manifest_key():
    return 'tags/' + storage._tag_embedding_index_manifest_blob_name('u')


def manifest(blobs):
    return json.loads(blobs.blobs[manifest_key()])


def assert_equivalent(source='v2'):
    loaded = storage._load_tag_embedding_index_npz('u')
    full = storage._build_user_tag_embedding_index_snapshot('u', source)
    assert loaded.source_version == source
    assert loaded.embedding_version == full.embedding_version
    assert loaded.tags == full.tags
    np.testing.assert_array_equal(loaded.embeddings, full.embeddings)


def test_default_full_format_and_no_sidecar(ctx):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    assert storage.refresh_user_tag_embedding_index('u', source_version='v1').tags == ['dog']
    assert 'contributionBlobName' not in manifest(blobs)
    assert sorted(blobs.blobs) == sorted(['tags/' + manifest(blobs)['npzBlobName'], manifest_key()])
    before = dict(blobs.blobs)
    assert refresh(['a'], incremental_only=False) is None  # No implicit full fallback.
    assert blobs.blobs == before


def test_bootstrap_one_existing_projected_scan(ctx, monkeypatch):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    seed(table, 'b', ['dog', 'beach'])
    calls = []
    original = table.query_entities
    def query(*args, **kwargs):
        calls.append(kwargs['select'])
        return original(*args, **kwargs)
    monkeypatch.setattr(table, 'query_entities', query)
    assert bootstrap().tags == ['beach', 'dog']
    assert calls == [storage._TAG_META_COLUMNS]
    published = manifest(blobs)
    with np.load(io.BytesIO(blobs.blobs['tags/' + published['npzBlobName']]), allow_pickle=False) as data:
        assert set(data.files) == {'tags', 'embeddings', 'source_version', 'embedding_version', 'updated_at'}
    assert_equivalent('v1')


def test_bootstrap_and_merge_report_encoding_and_transfer_progress(ctx, monkeypatch):
    table, blobs, encoded = ctx
    seed(table, 'a', ['dog'])
    phases = []
    original = incremental._write_output

    def output(*args, **kwargs):
        phases.append('output')
        return original(*args, **kwargs)

    monkeypatch.setattr(incremental, '_write_output', output)
    callbacks = []
    with index_progress.reporting_to(lambda: callbacks.append(phases[-1] if phases else 'scan')):
        assert bootstrap() is not None
        seed(table, 'b', ['beach'])
        assert refresh(['b']) is not None
    assert 'scan' in callbacks and 'output' in callbacks
    assert encoded == [['dog'], ['beach']]
    assert_equivalent()


def test_full_encoding_reports_progress_between_bounded_batches(ctx, monkeypatch):
    table, _, _ = ctx
    seed(table, 'a', ['dog'])
    tags = [f'tag-{i}' for i in range(incremental.ENCODE_BATCH * 2 + 1)]
    monkeypatch.setattr(storage, 'effective_tags', lambda row: tags)
    completed = []
    batches = []

    def encode(values):
        batches.append((len(values), len(completed)))
        return [[1.0, 0.0] for _ in values]

    monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', encode)
    with index_progress.reporting_to(lambda: completed.append(True)):
        result = storage._build_user_tag_embedding_index_snapshot('u', 'v1')
    assert result.tags == sorted(tags)
    assert [size for size, _ in batches] == [64, 64, 1]
    assert batches[0][1] < batches[1][1] < batches[2][1]


def test_add_remove_shared_tags_and_only_new_encodings(ctx, monkeypatch):
    table, blobs, encoded = ctx
    seed(table, 'a', ['dog', 'beach'])
    seed(table, 'b', ['dog'])
    assert bootstrap() is not None
    encoded.clear()
    table.delete_entity('u', 'a')
    seed(table, 'c', ['dog', 'new'])
    reads = []
    original = table.get_entity
    def get(**kwargs):
        reads.append(kwargs['row_key'])
        return original(**kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('full hydration/scan forbidden')
    with monkeypatch.context() as patch:
        patch.setattr(table, 'get_entity', get)
        patch.setattr(table, 'query_entities', forbidden)
        patch.setattr(storage, '_build_user_tag_embedding_index_snapshot', forbidden)
        patch.setattr(storage, '_load_tag_embedding_index_npz', forbidden)
        patch.setattr(np, 'load', forbidden)
        original_readall = _Downloaded.readall
        patch.setattr(_Downloaded, 'readall', lambda self: original_readall(self) if self._data.startswith(b'{') else forbidden())
        receipt = refresh(['a', 'c', 'c'])
    assert isinstance(receipt, incremental.TagEmbeddingPublication)
    assert receipt.tag_count == 2
    assert not hasattr(receipt, 'tags') and not hasattr(receipt, 'embeddings')
    assert reads == ['a', 'c']
    assert encoded == [['new']]
    assert 'u' not in storage._TAG_EMBEDDING_INDEX_CACHE
    assert_equivalent()
    # The last shared contribution disappears only when b AND c stop using it.
    table.delete_entity('u', 'b')
    seed(table, 'c', ['new'])
    assert refresh(['b', 'c'], source_version='v3', previous_source_version='v2').tag_count == 1
    assert_equivalent('v3')
    encoded.clear()
    seed(table, 'd', ['beach', 'dog'])
    assert refresh(['d'], source_version='v4', previous_source_version='v3').tag_count == 3
    assert encoded == []  # Reuse retained, zero-reference old vectors.
    assert_equivalent('v4')


def test_delete_all_empty_output_and_readd(ctx):
    table, _, encoded = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    table.delete_entity('u', 'a')
    assert refresh(['a']).tag_count == 0
    assert_equivalent()
    encoded.clear()
    seed(table, 'b', ['dog', 'cat'])
    assert refresh(['b'], source_version='v3', previous_source_version='v2').tag_count == 2
    assert encoded == [['cat']]
    assert_equivalent('v3')


def test_bootstrap_empty_then_add(ctx):
    table, _, _ = ctx
    assert bootstrap().tags == []
    seed(table, 'a', ['dog'])
    assert refresh(['a']).tag_count == 1
    assert_equivalent()


@pytest.mark.parametrize('metadata', [
    dict(deleted=True, faceCount=2, aiPersonLabel='friend', peopleNames='["Alice"]'),
    dict(subjectTags='["puppy", "dog"]', objects='["ball"]', backgroundTags='["garden"]'),
    dict(tags='malformed', subjectTags='invalid', processing_metadata='not-json'),
    dict(locationCity='Paris', address='France', exifData='{"GPSLatitude": 1}', tags='["Paris", "France", "dog"]'),
])
def test_effective_tags_exact_full_projection_semantics(ctx, metadata):
    table, _, _ = ctx
    seed(table, 'a', ['old'])
    bootstrap()
    table.upsert_entity(dict(PartitionKey='u', RowKey='a', **metadata))
    assert refresh(['a']) is not None
    assert_equivalent()


@pytest.mark.parametrize('change', ['model', 'schema', 'source', 'sidecar-version', 'missing-state', 'missing-npz', 'corrupt-state'])
def test_incompatible_or_missing_state_no_scan_no_publication(ctx, monkeypatch, change):
    table, blobs, encoded = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    value = manifest(blobs)
    if change in ('model', 'schema', 'source', 'sidecar-version'):
        field = {'model': 'embeddingVersion', 'schema': 'schemaVersion', 'source': 'sourceVersion',
                 'sidecar-version': 'contributionVersion'}[change]
        value[field] = 'incompatible'
        blobs.blobs[manifest_key()] = json.dumps(value).encode()
    elif change == 'corrupt-state':
        blobs.blobs['tags/' + value['contributionBlobName']] = b'bad sqlite'
    else:
        blobs.blobs.pop('tags/' + value['contributionBlobName' if change == 'missing-state' else 'npzBlobName'])
    before = dict(blobs.blobs)
    def forbidden(*args, **kwargs):
        raise AssertionError('incompatible base must defer before metadata reads')
    monkeypatch.setattr(table, 'get_entity', forbidden)
    monkeypatch.setattr(table, 'query_entities', forbidden)
    encoded.clear()
    assert refresh(['a']) is None
    assert blobs.blobs == before and not encoded


@pytest.mark.parametrize('failure', ['entity', 'table', 'download'])
def test_transient_and_missing_table_never_delete_or_publish(ctx, monkeypatch, failure):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = dict(blobs.blobs)
    def fail(*args, **kwargs):
        if failure == 'table':
            error = ResourceNotFoundError('missing table')
            error.error_code = 'TableNotFound'
            raise error
        raise TimeoutError('transient')
    monkeypatch.setattr(_Downloaded if failure == 'download' else table,
                        'readinto' if failure == 'download' else 'get_entity', fail)
    assert refresh(['a']) is None
    assert blobs.blobs == before


@pytest.mark.parametrize('failure', ['npz', 'sidecar', 'manifest', 'cas'])
@pytest.mark.parametrize('bootstrap_mode', [False, True])
def test_immutable_pair_then_cas_failure_never_reports_success(ctx, monkeypatch, failure, bootstrap_mode):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = blobs.blobs[manifest_key()]
    seed(table, 'a', ['cat'])
    original = _FakeBlob.upload_blob
    def upload(self, data, **kwargs):
        if self._key == manifest_key():
            if failure == 'manifest':
                raise IOError('manifest upload failed')
            if failure == 'cas':
                concurrent = json.loads(before)
                concurrent['sourceVersion'] = 'concurrent'
                blobs.blobs[manifest_key()] = json.dumps(concurrent).encode()
        elif self._key.endswith('.' + ('npz' if failure == 'npz' else 'sqlite')) and failure in ('npz', 'sidecar'):
            raise IOError('immutable data upload failed')
        return original(self, data, **kwargs)
    monkeypatch.setattr(_FakeBlob, 'upload_blob', upload)
    result = storage.refresh_user_tag_embedding_index('u', source_version='v2', model_version='clip-test',
                                                      bootstrap_incremental=True) if bootstrap_mode else refresh(['a'])
    assert result is None
    assert storage._load_tag_embedding_index_npz('u').source_version == 'v1'
    if failure != 'cas':
        assert blobs.blobs[manifest_key()] == before


def test_dirty_touches_follow_model_and_generation_provenance(ctx, monkeypatch):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = manifest(blobs)
    monkeypatch.setattr(storage, '_manifest_already_marked_dirty', lambda *args: False)
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'different-role-model')
    assert storage.touch_user_tag_embedding_index_state('u')
    assert storage.touch_user_tag_embedding_index_state('u')
    dirty = manifest(blobs)
    assert dirty['baseSourceVersion'] == 'v1'
    for field in ['embeddingVersion', 'schemaVersion', 'npzBlobName', 'contributionBlobName', 'contributionVersion']:
        assert dirty[field] == before[field]
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'clip-test')
    assert refresh(['a']) is not None
    assert storage.get_user_tag_embedding_index('u', allow_refresh=False)['tags'] == ['dog']
    assert storage.touch_user_tag_embedding_index_state('u')
    assert refresh(['a'], source_version='v3', previous_source_version='v2') is not None


@pytest.mark.parametrize('kind', ['ids', 'tags', 'text', 'dimensions', 'model', 'no-model', 'no-coverage', 'scalar', 'empty', 'bad-id'])
def test_resource_and_required_input_caps(ctx, monkeypatch, kind):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = dict(blobs.blobs)
    options = {}
    ids = ['a']
    if kind == 'ids':
        monkeypatch.setattr(incremental, 'MAX_IDS', 1)
        ids = iter(['a', 'b'])
    elif kind == 'tags':
        monkeypatch.setattr(incremental, 'MAX_TAGS_PER_PHOTO', 1)
        seed(table, 'a', ['dog', 'cat'])
    elif kind == 'text':
        seed(table, 'a', ['x' * (incremental.MAX_ITEM_CHARS + 1)])
    elif kind == 'dimensions':
        seed(table, 'a', ['cat'])
        monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', lambda tags: [[1] * (incremental.MAX_DIM + 1)])
    elif kind == 'model':
        options['model_version'] = 'wrong'
    elif kind == 'no-model':
        monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: False)
    elif kind == 'no-coverage':
        options['previous_source_version'] = None
    elif kind == 'scalar':
        ids = 'a'
    elif kind == 'empty':
        ids = []
    else:
        ids = ['x' * 1025]
    assert refresh(ids, **options) is None
    assert blobs.blobs == before


def test_bounded_batches_unicode_sorted_streaming_output(ctx, monkeypatch):
    table, _, encoded = ctx
    for index in range(1500):
        seed(table, f'photo-{index}', [f'tag{index:04}'])
    bootstrap()
    encoded.clear()
    for index in range(4):
        seed(table, f'new-{index}', [f'café🙂{index}x{tag}' for tag in range(20)])
    monkeypatch.setattr(incremental, 'ENCODE_BATCH', 7)
    receipt = refresh([f'new-{index}' for index in range(4)])
    assert receipt.tag_count == 1580
    assert sum(map(len, encoded)) == 80
    assert max(map(len, encoded)) <= 7
    assert_equivalent()


def test_corrupt_shared_refcount_defers(ctx, tmp_path):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    seed(table, 'b', ['dog'])
    bootstrap()
    key = 'tags/' + manifest(blobs)['contributionBlobName']
    path = tmp_path / 'bad.sqlite'
    path.write_bytes(blobs.blobs[key])
    with sqlite3.connect(path) as db:
        db.execute('UPDATE tags SET refs = 1')
    blobs.blobs[key] = path.read_bytes()
    before = dict(blobs.blobs)
    table.delete_entity('u', 'a')
    assert refresh(['a']) is None
    assert blobs.blobs == before


def test_owner_guard_is_not_bypassed(ctx, monkeypatch):
    from index_ownership import IndexOwnershipError
    monkeypatch.setattr(storage, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(storage, 'index_build_allowed', lambda: False)
    monkeypatch.setenv('APP_ROLE', 'core')
    with pytest.raises(IndexOwnershipError):
        bootstrap()
    with pytest.raises(IndexOwnershipError):
        refresh(['a'])


def test_delete_removes_current_published_pair(ctx):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    storage.delete_user_tag_embedding_index_data('u')
    assert not blobs.blobs


@pytest.mark.parametrize('bootstrap_mode', [False, True])
def test_model_change_during_encoding_defers(ctx, monkeypatch, bootstrap_mode):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = dict(blobs.blobs)
    seed(table, 'a', ['cat'])
    def changed(tags):
        monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'new-model')
        return [[1, 0] for tag in tags]
    monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', changed)
    if bootstrap_mode:
        result = storage.refresh_user_tag_embedding_index('u', source_version='v2', model_version='clip-test',
                                                          bootstrap_incremental=True)
    else:
        result = refresh(['a'])
    assert result is None and blobs.blobs == before


def test_touch_cas_cannot_regress_generation(ctx, monkeypatch):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    original = _FakeBlob.upload_blob
    monkeypatch.setattr(storage, '_manifest_already_marked_dirty', lambda *args: False)
    def race(self, data, **kwargs):
        if self._key == manifest_key():
            newer = manifest(blobs)
            newer['sourceVersion'] = 'concurrent'
            blobs.blobs[manifest_key()] = json.dumps(newer).encode()
        return original(self, data, **kwargs)
    monkeypatch.setattr(_FakeBlob, 'upload_blob', race)
    assert storage.touch_user_tag_embedding_index_state('u') == ''
    assert manifest(blobs)['sourceVersion'] == 'concurrent'
    assert not manifest(blobs)['dirty']


def test_bootstrap_scan_failure_cannot_publish_partial_contributions(ctx, monkeypatch):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    def broken(*args, **kwargs):
        yield dict(PartitionKey='u', RowKey='a', tags='["dog"]')
        raise TimeoutError('scan interrupted')
    monkeypatch.setattr(table, 'query_entities', broken)
    assert bootstrap() is None
    assert not blobs.blobs


@pytest.mark.parametrize('response', [[], [[1, 2, 3]], [[float('nan'), 1]], [[1, 2], [3, 4]]])
def test_invalid_encoder_response_never_publishes(ctx, monkeypatch, response):
    table, blobs, _ = ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = dict(blobs.blobs)
    seed(table, 'b', ['cat'])
    monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', lambda tags: response)
    assert refresh(['b']) is None
    assert blobs.blobs == before


def test_incremental_allocations_do_not_scale_with_library_or_output(ctx, monkeypatch):
    """Exclude test blob-store buffering; production receives seekable streams.

    Sidecar IO and NPZ generation for 32x the photos/tags must still have a
    fixed Python allocation ceiling. SQLite has a fixed 2 MiB page cache and
    file-backed temp tables, rather than vocabulary-sized Python containers.
    """
    import tracemalloc
    table, _, _ = ctx
    original = _FakeBlob.upload_blob
    peaks = []
    for count in (128, 4096):
        table.rows.clear()
        for index in range(count):
            seed(table, f'photo{index}', [f'tag{index:05}'])
        assert bootstrap() is not None
        chunk_sizes = []
        def upload(self, data, **kwargs):
            if kwargs.get('overwrite') is False and hasattr(data, 'read'):
                # Stream to a discard sink, not the unbounded in-memory fake.
                while True:
                    chunk = data.read(65536)
                    if not chunk:
                        break
                    chunk_sizes.append(len(chunk))
                return None
            return original(self, data, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(_FakeBlob, 'upload_blob', upload)
            tracemalloc.start()
            try:
                result = refresh(['photo0'])
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
        assert result.tag_count == count
        assert max(chunk_sizes) <= 65536
    assert peaks[1] < peaks[0] + 512 * 1024
    assert max(peaks) < 2 * 1024 * 1024


def rebootstrap(source):
    return storage.refresh_user_tag_embedding_index('u', source_version=source, model_version='clip-test',
                                                     bootstrap_incremental=True)


def test_rebootstrap_encodes_only_new_tags_and_matches_full_encoding(ctx):
    table, _, encoded = ctx
    seed(table, 'a.jpg', ['dog', 'beach'])
    seed(table, 'b.jpg', ['dog'])
    assert bootstrap() is not None
    encoded.clear()
    seed(table, 'c.jpg', ['sunset', 'dog'])
    assert rebootstrap('v2') is not None
    assert encoded == [['sunset']]
    assert_equivalent('v2')


def test_rebootstrap_reuses_everything_when_vocabulary_unchanged(ctx):
    table, _, encoded = ctx
    seed(table, 'a.jpg', ['dog', 'beach'])
    assert bootstrap() is not None
    encoded.clear()
    seed(table, 'b.jpg', ['dog'])
    assert rebootstrap('v2') is not None
    assert encoded == []
    assert_equivalent('v2')


def test_rebootstrap_encodes_everything_when_model_changed(ctx, monkeypatch):
    table, _, encoded = ctx
    seed(table, 'a.jpg', ['dog'])
    assert bootstrap() is not None
    encoded.clear()
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'clip-next')
    assert storage.refresh_user_tag_embedding_index(
        'u', source_version='v2', model_version='clip-next', bootstrap_incremental=True) is not None
    assert encoded == [['dog']]


def test_rebootstrap_ignores_corrupt_sidecar_and_encodes_all(ctx):
    table, blobs, encoded = ctx
    seed(table, 'a.jpg', ['dog', 'beach'])
    assert bootstrap() is not None
    blobs.blobs['tags/' + manifest(blobs)['contributionBlobName']] = b'not sqlite'
    encoded.clear()
    assert rebootstrap('v2') is not None
    assert encoded == [['beach', 'dog']]
    assert_equivalent('v2')
