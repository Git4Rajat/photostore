"""Opt-in vector merges: bounded IO, full equivalence and fail-closed publication."""
import io
import json
from types import SimpleNamespace
import uuid

import numpy as np
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError, ResourceExistsError

import storage_utils as su
import vector_incremental as incremental
from index_work import DirtySnapshot
from tests.fakes import FakeTable


class Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data

    def readinto(self, stream):
        for offset in range(0, len(self.data), 65536):
            stream.write(self.data[offset:offset + 65536])


class Blob:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def get_blob_properties(self):
        if self.name not in self.service.data:
            raise ResourceNotFoundError(self.name)
        data = self.service.data[self.name]
        return SimpleNamespace(size=len(data), etag=str(hash(data)))

    def check(self, kwargs):
        if 'etag' in kwargs:
            assert kwargs['match_condition'] == MatchConditions.IfNotModified
            if kwargs['etag'] != self.get_blob_properties().etag:
                raise ResourceModifiedError('ETag conflict')

    def download_blob(self, **kwargs):
        self.check(kwargs)
        self.get_blob_properties()
        return Download(self.service.data[self.name])

    def upload_blob(self, data, overwrite=True, **kwargs):
        self.check(kwargs)
        if not overwrite and self.name in self.service.data:
            raise ResourceExistsError(self.name)
        self.service.data[self.name] = data.read() if hasattr(data, 'read') else data


class Service:
    def __init__(self):
        self.data = {}

    def get_blob_client(self, container, blob):
        return Blob(self, container + '/' + blob)


@pytest.fixture
def ctx(monkeypatch):
    metadata, embeddings, service = FakeTable(), FakeTable(), Service()
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', FakeTable())
    monkeypatch.setitem(su._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(su._CTX, 'embeddings_table_client', embeddings)
    monkeypatch.setitem(su._CTX, 'blob_service_client', service)
    monkeypatch.setitem(su._CTX, 'blob_vector_index_container', 'vector-index')
    monkeypatch.setattr(su, 'PHOTO_EMBEDDING_DIMENSION', 3)
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_dimension', lambda: 3)
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_version', lambda: 'model-A')
    monkeypatch.setattr(su.vision_utils, 'encode_text_embedding', lambda text: [1, 2, 3])
    invalidate = su.invalidate_user_vector_index_cache
    invalidate('u1')
    yield metadata, embeddings, service
    invalidate('u1')


def seed(table, name, vector=None, **kwargs):
    row = {'PartitionKey': 'u1', 'RowKey': name, **kwargs}
    if vector is not None:
        row.update(photoEmbedding=json.dumps(vector), photoEmbeddingVersion=su.PHOTO_EMBEDDING_MODEL_VERSION)
    table.upsert_entity(row)


def base(ctx):
    metadata, embeddings, _ = ctx
    seed(metadata, 'a.jpg', [0, 1, 0])
    seed(metadata, 'b.jpg')
    seed(embeddings, 'b.jpg', [1, 0, 0])
    assert su.refresh_user_vector_index('u1', source_version='v1') is not None


def refresh(ids, **kwargs):
    options = dict(source_version='v2', affected_filenames=ids,
                   previous_source_version='v1', incremental_only=True)
    options.update(kwargs)
    return su.refresh_user_vector_index('u1', **options)


def manifest_key():
    return 'vector-index/' + su._vector_index_manifest_blob_name('u1')


def data_key():
    manifest = su._load_vector_index_manifest('u1')
    return 'vector-index/' + (manifest.get('blobName') or su._vector_index_npz_blob_name('u1'))


def assert_equivalent(tmp_path):
    loaded = su._load_vector_index_npz('u1')
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    built = su._build_user_vector_index_file('u1', loaded.source_version, str(directory), str(directory))
    assert built is not None
    with np.load(built[0], allow_pickle=False) as data:
        keys = json.loads(str(data['row_keys'][0]))
        expected = dict(zip(keys, data['embeddings']))
        assert loaded.embeddings.shape == data['embeddings'].shape
        actual = dict(zip(loaded.row_keys, loaded.embeddings))
        assert actual.keys() == expected.keys()
        for key in expected:
            np.testing.assert_array_equal(actual[key], expected[key])


def forbidden(*args, **kwargs):
    raise AssertionError('whole-index hydration/full fallback forbidden')


def test_add_update_delete_without_hydration(ctx, monkeypatch, tmp_path):
    base(ctx)
    metadata, embeddings, _ = ctx
    metadata.delete_entity('u1', 'a.jpg')
    seed(embeddings, 'b.jpg', [0, 0, 4])
    seed(metadata, '新🙂.jpg', [4, 0, 0])
    reads = []
    original = metadata.get_entity
    monkeypatch.setattr(metadata, 'get_entity', lambda **kw: (reads.append(kw['row_key']), original(**kw))[1])
    with monkeypatch.context() as patch:
        patch.setattr(metadata, 'query_entities', forbidden)
        patch.setattr(embeddings, 'query_entities', forbidden)
        patch.setattr(su, '_build_user_vector_index_file', forbidden)
        patch.setattr(su, '_load_vector_index_npz', forbidden)
        patch.setattr(np, 'load', forbidden)
        readall = Download.readall
        patch.setattr(Download, 'readall', lambda self: readall(self) if self.data.startswith(b'{') else forbidden())
        receipt = refresh(['a.jpg', 'b.jpg', '新🙂.jpg', 'b.jpg'])
    assert isinstance(receipt, incremental.VectorPublication)
    assert receipt.row_count == 2
    assert not hasattr(receipt, 'row_keys') and not hasattr(receipt, 'embeddings')
    assert sorted(reads) == ['a.jpg', 'b.jpg', '新🙂.jpg']
    assert su.get_vector_index_blob_location('u1')[1] == receipt.blob_name
    assert su.get_user_vector_index('u1', allow_refresh=False)['source_version'] == 'v2'
    assert_equivalent(tmp_path)


@pytest.mark.parametrize('kind', ['metadata', 'embeddings', 'table_missing', 'compute', 'download'])
def test_transient_errors_defer_not_delete(ctx, monkeypatch, kind):
    base(ctx)
    metadata, embeddings, service = ctx
    before = dict(service.data)
    def fail(*args, **kwargs):
        if kind == 'table_missing':
            error = ResourceNotFoundError('table missing')
            error.error_code = 'TableNotFound'
            raise error
        raise TimeoutError('transient failure')
    if kind in ('metadata', 'table_missing'):
        monkeypatch.setattr(metadata, 'get_entity', fail)
    elif kind == 'embeddings':
        monkeypatch.setattr(embeddings, 'get_entity', fail)
    elif kind == 'compute':
        monkeypatch.setattr(su, '_text_photo_vector', fail)
        embeddings.rows.clear()
    else:
        monkeypatch.setattr(Download, 'readinto', fail)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    assert refresh(['b.jpg']) is None
    assert service.data == before


@pytest.mark.parametrize('change', ['missing_manifest', 'missing_blob', 'schema', 'model', 'source',
                                    'npz_model', 'npz_source', 'dtype', 'dimension', 'keys', 'rowcount', 'corrupt'])
def test_missing_incompatible_base_strictly_defers(ctx, monkeypatch, change):
    base(ctx)
    _, _, service = ctx
    mk, dk = manifest_key(), data_key()
    if change == 'missing_manifest':
        service.data.pop(mk)
    elif change == 'missing_blob':
        service.data.pop(dk)
    elif change in ('schema', 'model', 'source', 'rowcount'):
        manifest = json.loads(service.data[mk])
        field, value = {'schema': ('schemaVersion', 99), 'model': ('embeddingVersion', 'B'),
                        'source': ('sourceVersion', 'old'), 'rowcount': ('rowCount', 99)}[change]
        manifest[field] = value
        service.data[mk] = json.dumps(manifest).encode()
    elif change == 'corrupt':
        service.data[dk] = b'corrupt'
    else:
        with np.load(io.BytesIO(service.data[dk]), allow_pickle=False) as data:
            members = dict(data)
        if change in ('npz_model', 'npz_source'):
            members['embedding_version' if change == 'npz_model' else 'source_version'] = np.asarray(['B'])
        elif change == 'dtype':
            members['embeddings'] = members['embeddings'].astype('float64')
        elif change == 'dimension':
            members['embeddings'] = members['embeddings'][:, :2]
        else:
            members['row_keys'] = np.asarray(['["a.jpg","a.jpg"]'])
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **members)
        service.data[dk] = buffer.getvalue()
    before = dict(service.data)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    assert refresh(['b.jpg'], incremental_only=False) is None
    assert service.data == before


@pytest.mark.parametrize('failure', ['data', 'manifest', 'cas'])
@pytest.mark.parametrize('strict', [True, False])
def test_publication_failure_no_fallback(ctx, monkeypatch, failure, strict):
    base(ctx)
    _, _, service = ctx
    original = Blob.upload_blob
    before = service.data[manifest_key()]
    def upload(self, data, **kwargs):
        if kwargs.get('overwrite') is False:
            if failure == 'data':
                raise IOError('data upload failed')
            if failure == 'cas':
                newer = json.loads(before)
                newer['sourceVersion'] = 'concurrent'
                service.data[manifest_key()] = json.dumps(newer).encode()
        elif self.name == manifest_key() and failure == 'manifest':
            raise IOError('manifest upload failed')
        return original(self, data, **kwargs)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    assert refresh(['b.jpg'], incremental_only=strict) is None
    assert su._load_vector_index_npz('u1').source_version == 'v1'
    if failure != 'cas':
        assert service.data[manifest_key()] == before


def test_dirty_touch_preserves_provenance_across_merges(ctx):
    base(ctx)
    su.touch_user_vector_index_state('u1', embedding_version='other-process-model')
    dirty = su._load_vector_index_manifest('u1')
    assert dirty['baseSourceVersion'] == 'v1'
    assert dirty['embeddingVersion'] == 'model-A'
    receipt = refresh(['a.jpg'])
    assert receipt is not None
    su.touch_user_vector_index_state('u1')
    dirty = su._load_vector_index_manifest('u1')
    assert dirty['blobName'] == receipt.blob_name
    assert dirty['baseSourceVersion'] == 'v2'
    su._clear_manifest_dirty_flag('u1', 'vector')
    su.touch_user_vector_index_state('u1')  # Simulate another process touching an already dirty base.
    assert su._load_vector_index_manifest('u1')['baseSourceVersion'] == 'v2'
    assert refresh(['b.jpg'], source_version='v3', previous_source_version='v2') is not None
    assert su._load_vector_index_npz('u1').source_version == 'v3'


def test_failed_dirty_touch_retries_and_does_not_clobber_pointer(ctx, monkeypatch):
    base(ctx)
    receipt = refresh(['a.jpg'])
    with monkeypatch.context() as patch:
        patch.setattr(Blob, 'upload_blob', forbidden)
        su.touch_user_vector_index_state('u1')
    assert ('u1', 'vector') not in su._INDEX_MANIFEST_DIRTY_FLAGS
    assert su.touch_user_vector_index_state('u1')
    assert su._load_vector_index_manifest('u1')['blobName'] == receipt.blob_name


@pytest.mark.parametrize('ids', [[], 'a.jpg', [None], [''], ['x' * 1025]])
def test_invalid_ids_defer(ctx, monkeypatch, ids):
    base(ctx)
    before = dict(ctx[2].data)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    assert refresh(ids) is None
    assert ctx[2].data == before


def test_budget_stops_consuming_unbounded_ids(ctx, monkeypatch):
    base(ctx)
    monkeypatch.setattr(incremental, 'MAX_IDS', 2)
    consumed = []
    def ids():
        for index in range(1000):
            consumed.append(index)
            yield str(index)
    assert refresh(ids()) is None
    assert consumed == [0, 1, 2]


def test_source_field_budget_defers_before_json_hydration(ctx, monkeypatch):
    base(ctx)
    ctx[1].rows[('u1', 'b.jpg')]['photoEmbedding'] = ' ' * (incremental.MAX_ITEM_CHARS + 1)
    before = dict(ctx[2].data)
    monkeypatch.setattr(su, '_stored_photo_vector', forbidden)
    assert refresh(['b.jpg']) is None
    assert ctx[2].data == before


def test_dirty_touch_racing_publication_fences_merge(ctx, monkeypatch):
    base(ctx)
    before = su._load_vector_index_manifest('u1')['blobName']
    original = Blob.upload_blob
    def upload(self, data, **kwargs):
        if kwargs.get('overwrite') is False and self.name.endswith('.npz'):
            su.touch_user_vector_index_state('u1')
        return original(self, data, **kwargs)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    assert refresh(['a.jpg']) is None
    manifest = su._load_vector_index_manifest('u1')
    assert manifest['dirty'] is True
    assert manifest['baseSourceVersion'] == 'v1'
    assert manifest['blobName'] == before
    assert su._load_vector_index_npz('u1').source_version == 'v1'


def test_failed_touch_cas_preserves_concurrent_committed_pointer(ctx, monkeypatch):
    base(ctx)
    receipt = refresh(['a.jpg'])
    original = Blob.upload_blob
    def upload(self, data, **kwargs):
        if self.name == manifest_key():
            concurrent = json.loads(self.service.data[self.name])
            concurrent['blobName'] = 'concurrent.npz'
            self.service.data[self.name] = json.dumps(concurrent).encode()
        return original(self, data, **kwargs)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    su.touch_user_vector_index_state('u1')
    assert su._load_vector_index_manifest('u1')['blobName'] == 'concurrent.npz'
    assert ('u1', 'vector') not in su._INDEX_MANIFEST_DIRTY_FLAGS
    assert receipt is not None


def test_explicit_ids_and_previous_source_required_no_auto_dirty_integration(ctx, monkeypatch):
    base(ctx)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', forbidden)
    assert su.refresh_user_vector_index('u1', incremental_only=True) is None
    assert refresh(['a.jpg'], previous_source_version=None) is None
    assert refresh(['a.jpg'], source_version='v1') is None
    assert refresh(['a.jpg'], force_full=True) is None
    assert refresh(['a.jpg']) is not None


@pytest.mark.parametrize('embedding_case', ['wrong_model', 'malformed', 'zero', 'missing', 'inline', 'deleted'])
def test_full_vector_semantics_equivalence(ctx, embedding_case, tmp_path):
    base(ctx)
    metadata, embeddings, _ = ctx
    seed(metadata, 'b.jpg', [0, 1, 0])
    if embedding_case == 'wrong_model':
        embeddings.rows[('u1', 'b.jpg')]['photoEmbeddingVersion'] = 'wrong'
    elif embedding_case == 'malformed':
        embeddings.rows[('u1', 'b.jpg')]['photoEmbedding'] = 'bad JSON'
    elif embedding_case == 'zero':
        seed(embeddings, 'b.jpg', [0, 0, 0])
    elif embedding_case in ('missing', 'inline'):
        embeddings.rows.clear()
        if embedding_case == 'missing':
            metadata.rows[('u1', 'b.jpg')].pop('photoEmbedding')
    else:
        metadata.rows[('u1', 'b.jpg')]['processing_state'] = ' DELETED '
    assert refresh(['b.jpg']) is not None
    assert_equivalent(tmp_path)


def test_empty_then_readd_and_legacy_loader_fallback(ctx, tmp_path):
    base(ctx)
    # Simulate a pre-rollout manifest with only the legacy fixed alias.
    service = ctx[2]
    service.data['vector-index/' + su._vector_index_npz_blob_name('u1')] = service.data[data_key()]
    manifest = su._load_vector_index_manifest('u1')
    manifest.pop('blobName')
    service.data[manifest_key()] = json.dumps(manifest).encode()
    assert su.get_vector_index_blob_location('u1')[1] == su._vector_index_npz_blob_name('u1')
    metadata, _, _ = ctx
    metadata.rows.clear()
    assert refresh(['a.jpg', 'b.jpg']).row_count == 0
    assert_equivalent(tmp_path)
    seed(metadata, 'c.jpg', [1, 0, 0])
    assert refresh(['c.jpg'], source_version='v3', previous_source_version='v2').row_count == 1
    assert_equivalent(tmp_path)


def test_full_builder_remains_default_even_with_dirty_marks(ctx, monkeypatch, tmp_path):
    base(ctx)
    seed(ctx[0], 'new.jpg', [0, 1, 0])
    monkeypatch.setattr(incremental, 'merge_and_publish', forbidden)
    result = su.refresh_user_vector_index('u1', source_version='v2', force_full=True)
    assert isinstance(result, su.VectorIndexSnapshot)
    assert result.row_keys == ['a.jpg', 'b.jpg', 'new.jpg']
    assert su._load_vector_index_manifest('u1')['blobName'].endswith('.npz')
    assert_equivalent(tmp_path)


def test_full_after_incremental_replaces_pointer(ctx, tmp_path):
    base(ctx)
    receipt = refresh(['a.jpg'])
    old_data = ctx[2].data[data_key()]
    seed(ctx[0], 'new.jpg', [0, 1, 0])
    result = su.refresh_user_vector_index('u1', source_version='v3')
    assert result is not None and result.embeddings.size == 0
    assert result.row_keys == ['a.jpg', 'b.jpg', 'new.jpg']
    manifest = su._load_vector_index_manifest('u1')
    assert manifest['blobName'] != receipt.blob_name
    assert manifest['sourceVersion'] == 'v3' and not manifest['dirty']
    assert ctx[2].data['vector-index/' + receipt.blob_name] == old_data
    assert su.get_vector_index_blob_location('u1')[1] == manifest['blobName']
    assert su.get_user_vector_index('u1', allow_refresh=False)['source_version'] == 'v3'
    assert_equivalent(tmp_path)


@pytest.mark.parametrize('failure', ['data', 'manifest', 'missing_data', 'missing_manifest', 'properties'])
def test_full_publication_failure_retains_dirty_capture_and_cache(ctx, monkeypatch, failure):
    base(ctx)
    assert refresh(['a.jpg']) is not None
    su.touch_user_vector_index_state('u1')
    before = ctx[2].data[manifest_key()]
    capture = DirtySnapshot([{'PartitionKey': 'u1:vector', 'RowKey': 'a.jpg'}])
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a: capture)
    monkeypatch.setattr(su, '_clear_dirty_search_index_filenames', forbidden)
    monkeypatch.setattr(su, 'invalidate_user_vector_index_cache', forbidden)
    original = Blob.upload_blob
    def upload(self, data, **kwargs):
        if ((failure == 'data' and self.name.endswith('.npz')) or
                (failure == 'manifest' and self.name == manifest_key())):
            raise IOError('publication failed')
        return original(self, data, **kwargs)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    client = su._vector_index_blob_client
    if failure.startswith('missing_'):
        monkeypatch.setattr(su, '_vector_index_blob_client', lambda name:
                            None if name.endswith('.npz' if failure == 'missing_data' else '.json') else client(name))
    elif failure == 'properties':
        monkeypatch.setattr(Blob, 'get_blob_properties', forbidden)
    assert su.refresh_user_vector_index('u1', source_version='v3') is None
    assert ctx[2].data[manifest_key()] == before
    assert capture == {'a.jpg'}


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('race', ['build', 'data'])
def test_full_capture_before_build_fences_concurrent_touch(ctx, monkeypatch, existing, race):
    if existing:
        base(ctx)
        assert refresh(['a.jpg']) is not None
        su.touch_user_vector_index_state('u1')  # Already-dirty dedup must be re-armed.
    built, upload = su._build_user_vector_index_file, Blob.upload_blob
    def build(*args):
        if race == 'build':
            su.touch_user_vector_index_state('u1')
        return built(*args)
    def write(self, data, **kwargs):
        if race == 'data' and self.name.endswith('.npz'):
            su.touch_user_vector_index_state('u1')
        return upload(self, data, **kwargs)
    monkeypatch.setattr(su, '_build_user_vector_index_file', build)
    monkeypatch.setattr(Blob, 'upload_blob', write)
    monkeypatch.setattr(su, '_clear_dirty_search_index_filenames', forbidden)
    # A touch itself invalidates cache; only forbid the builder's success path.
    invalidated = []
    monkeypatch.setattr(su, 'invalidate_user_vector_index_cache', lambda user: invalidated.append(user))
    assert su.refresh_user_vector_index('u1', source_version='v3') is None
    manifest = su._load_vector_index_manifest('u1')
    assert manifest['dirty'] is True
    if existing:
        assert manifest['baseSourceVersion'] == 'v2'
        assert su._load_vector_index_npz('u1').source_version == 'v2'
    assert invalidated == ['u1']


def test_full_ack_uses_original_capture_only_after_manifest_commit(ctx, monkeypatch):
    base(ctx)
    capture = DirtySnapshot([{'PartitionKey': 'u1:vector', 'RowKey': 'a.jpg'}])
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a: capture)
    acknowledged = []
    def ack(user, kind, snapshot):
        assert snapshot is capture
        assert su._load_vector_index_manifest(user)['sourceVersion'] == 'v2'
        assert su._load_vector_index_npz(user).source_version == 'v2'
        acknowledged.append(snapshot)
    monkeypatch.setattr(su, '_clear_dirty_search_index_filenames', ack)
    assert su.refresh_user_vector_index('u1', source_version='v2') is not None
    assert acknowledged == [capture]


@pytest.mark.parametrize('remark', [False, True])
def test_full_dirty_ack_retains_concurrent_row_versions(ctx, monkeypatch, remark):
    base(ctx)
    table = su._CTX['search_index_dirty_table_client']
    partition = su._search_index_dirty_partition_key('u1', 'vector')
    row = {'PartitionKey': partition, 'RowKey': 'a.jpg'}
    table.upsert_entity(row)
    original = Blob.upload_blob
    def upload(self, data, **kwargs):
        result = original(self, data, **kwargs)
        if self.name == manifest_key() and remark:
            # A new mutation after commit must survive both conditional dirty
            # acknowledgment and the builder's dirty-dedup lifecycle.
            table.upsert_entity(row)
            su.touch_user_vector_index_state('u1')
        return result
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    assert su.refresh_user_vector_index('u1', source_version='v2') is not None
    assert su._get_dirty_search_index_filenames('u1', 'vector') == ({'a.jpg'} if remark else set())
    assert bool(su._load_vector_index_manifest('u1')['dirty']) == remark
    assert (('u1', 'vector') in su._INDEX_MANIFEST_DIRTY_FLAGS) == remark


def test_full_manifest_failure_propagates_under_publication_guard(ctx, monkeypatch):
    import index_publication
    base(ctx)
    original = Blob.upload_blob
    def upload(self, data, **kwargs):
        if self.name == manifest_key():
            raise IOError('manifest unavailable')
        return original(self, data, **kwargs)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    guard = SimpleNamespace(check=lambda: None)
    with index_publication.guarded_publication(guard):
        with pytest.raises(IOError, match='manifest unavailable'):
            su.refresh_user_vector_index('u1', source_version='v2')
    assert su._load_vector_index_npz('u1').source_version == 'v1'


@pytest.mark.parametrize('flag', ['has_more', 'truncated'])
def test_incomplete_capture_rejected_before_iteration(ctx, monkeypatch, flag):
    base(ctx)
    class Capture:
        def __iter__(self):
            forbidden()
    capture = Capture()
    setattr(capture, flag, True)
    before = dict(ctx[2].data)
    monkeypatch.setattr(su, '_build_user_vector_index_file', forbidden)
    assert refresh(capture) is None
    assert ctx[2].data == before


@pytest.mark.parametrize('pointer', ['', None, False, 123, [], {}, 'other-user.npz',
                                     '{prefix}evil.npz', '{prefix}.output.zip', '{prefix}.npz/other'])
def test_invalid_present_pointer_rejected_by_all_vector_readers(ctx, monkeypatch, pointer):
    base(ctx)
    if isinstance(pointer, str):
        pointer = pointer.format(prefix=su._vector_index_blob_key('u1'))
    manifest = su._load_vector_index_manifest('u1')
    manifest['blobName'] = pointer
    ctx[2].data[manifest_key()] = json.dumps(manifest).encode()
    original = su._vector_index_blob_client
    monkeypatch.setattr(su, '_vector_index_blob_client', lambda name:
                        original(name) if name.endswith('.json') else forbidden())
    assert su._load_vector_index_npz('u1') is None
    with pytest.raises(ValueError, match='blob pointer'):
        su.get_vector_index_blob_location('u1')
    before = dict(ctx[2].data)
    assert refresh(['a.jpg']) is None
    assert ctx[2].data == before


def test_large_keys_streamed_with_bounded_json_and_no_mmap(ctx, monkeypatch):
    base(ctx)
    _, _, service = ctx
    count = 12000
    names = [f'写真🙂-{index:06d}.jpg' for index in range(count)]
    snapshot = su.VectorIndexSnapshot('u1', 'v1', 'model-A', 'v1', names,
                                      np.ones((count, 3), dtype=np.float32))
    service.data[data_key()] = su._serialize_vector_index(snapshot)
    manifest = su._load_vector_index_manifest('u1')
    manifest['rowCount'] = count
    service.data[manifest_key()] = json.dumps(manifest).encode()
    original = json.loads
    def bounded_json(text, *args, **kwargs):
        assert len(text) < 5000
        return original(text, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(json, 'loads', bounded_json)
        patch.setattr(np, 'load', forbidden)
        patch.setattr(np, 'memmap', forbidden)
        receipt = refresh(['a.jpg'])
    assert receipt.row_count == count + 1
    loaded = su._load_vector_index_npz('u1')
    assert loaded.row_keys[:-1] == names