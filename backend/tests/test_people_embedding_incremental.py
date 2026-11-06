"""Incremental publication is opt-in, bounded, equivalent, and fail-closed."""
import io
import json
import zipfile

import numpy as np
import pytest

import people_embedding_incremental as incremental
import storage_utils
from tests.test_people_embedding_index import (
    ctx, _seed_person, _seed_face, _FakeBlob, _Downloaded,
)


def seed_base(ctx):
    people, faces, _ = ctx
    for pid, fid, vector in [('p1', 'f1', [1, 2, 3]), ('p2', 'f2', [4])]:
        _seed_person(people, 'u1', pid, [fid], vector, name=pid)
        _seed_face(faces, 'u1', fid, pid)
    assert storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v1', model_version='model-A') is not None


def refresh(ids, **kwargs):
    return storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v2', affected_person_ids=ids, incremental_only=True,
        previous_source_version='v1', model_version='model-A', **kwargs)


def assert_equivalent():
    loaded = storage_utils._load_people_embedding_index_npz('u1')
    full = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v2')
    assert loaded.embeddings.shape == full.embeddings.shape
    def indexed(snapshot):
        return {pid: (snapshot.people_meta[i], snapshot.embeddings[i].tolist())
                for i, pid in enumerate(snapshot.person_ids)}
    assert indexed(loaded) == indexed(full)


def test_add_update_delete_mixed_width_equivalence(ctx, monkeypatch):
    seed_base(ctx)
    people, faces, _ = ctx
    people.delete_entity('u1', 'p1')  # Deleting widest rep must shrink matrix.
    _seed_person(people, 'u1', 'p2', ['f2', 'f2'], [8, 9], name='updated')
    _seed_person(people, 'u1', 'p3', ['f3'], [7], name='added')
    _seed_face(faces, 'u1', 'f3', 'p3')
    # No full scans, even of affected people; no old matrix/key-list hydration.
    def forbidden(*args, **kwargs):
        raise AssertionError('whole-index read/scan forbidden')
    with monkeypatch.context() as patch:
        patch.setattr(people, 'query_entities', forbidden)
        patch.setattr(faces, 'query_entities', forbidden)
        patch.setattr(storage_utils, '_load_people_embedding_index_npz', forbidden)
        patch.setattr(np, 'load', forbidden)
        original = _Downloaded.readall
        patch.setattr(_Downloaded, 'readall', lambda self: original(self) if self._data.startswith(b'{') else forbidden())
        result = refresh(['p1', 'p2', 'p3'])
    assert isinstance(result, incremental.PeopleEmbeddingPublication)
    assert result.row_count == 2
    assert 'u1' not in storage_utils._PEOPLE_EMBEDDING_INDEX_CACHE
    assert_equivalent()


@pytest.mark.parametrize('rejection', ['boolean', 'status', 'ownership', 'missing'])
def test_face_rejection_ownership_and_missing_equivalence(ctx, rejection):
    seed_base(ctx)
    people, faces, _ = ctx
    row = faces.rows[('u1', 'f1')]
    if rejection == 'boolean':
        row['rejected'] = True
    elif rejection == 'status':
        row['reviewStatus'] = ' REJECTED '
    elif rejection == 'ownership':
        row['personId'] = 'p2'
    else:
        faces.delete_entity('u1', 'f1')
    assert refresh(['p1']) is not None
    assert_equivalent()


@pytest.mark.parametrize('table_name', ['person', 'face'])
def test_transient_target_read_does_not_delete_or_publish(ctx, monkeypatch, table_name):
    seed_base(ctx)
    people, faces, blobs = ctx
    before = dict(blobs.blobs)
    table = people if table_name == 'person' else faces
    def fail(**kwargs):
        raise TimeoutError('temporary service failure')
    monkeypatch.setattr(table, 'get_entity', fail)
    assert refresh(['p1']) is None
    assert blobs.blobs == before


@pytest.mark.parametrize('change', ['missing', 'schema', 'model', 'source', 'widths', 'matrix', 'rowcount'])
def test_missing_incompatible_and_corrupt_base_defer(ctx, change):
    seed_base(ctx)
    _, _, blobs = ctx
    manifest_key = 'people-embedding-index/' + storage_utils._people_embedding_index_manifest_blob_name('u1')
    key = 'people-embedding-index/' + json.loads(blobs.blobs[manifest_key])['npzBlobName']
    if change == 'missing':
        blobs.blobs.pop(key)
    elif change in ('schema', 'model', 'source', 'rowcount'):
        manifest = json.loads(blobs.blobs[manifest_key])
        field, value = {'schema': ('embeddingVersion', 'future'), 'model': ('modelVersion', 'B'),
                        'source': ('sourceVersion', 'old'), 'rowcount': ('rowCount', 100)}[change]
        manifest[field] = value
        blobs.blobs[manifest_key] = json.dumps(manifest).encode()
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blobs.blobs[key])) as source, zipfile.ZipFile(output, 'w') as target:
            for name in source.namelist():
                if change == 'widths' and name == 'rep_sizes.npy':
                    continue
                target.writestr(name, b'corrupt' if change == 'matrix' and name == 'embeddings.npy' else source.read(name))
        blobs.blobs[key] = output.getvalue()
    before = dict(blobs.blobs)
    assert refresh(['p1']) is None
    assert blobs.blobs == before


def test_non_strict_missing_base_falls_back_to_full_builder(ctx):
    people, faces, _ = ctx
    _seed_person(people, 'u1', 'p1', ['f1'], [1])
    _seed_face(faces, 'u1', 'f1', 'p1')
    result = storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v1', affected_person_ids=['p1'], model_version='model-A')
    assert result.person_ids == ['p1']
    assert refresh(['p1']) is not None  # Bootstrap has merge provenance.


def test_dirty_manifest_retains_base_and_new_pointer(ctx):
    seed_base(ctx)
    storage_utils.touch_user_people_embedding_index_state('u1')
    assert refresh(['p1']) is not None
    storage_utils.touch_user_people_embedding_index_state('u1')
    result = storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v3', affected_person_ids=['p2'], incremental_only=True,
        previous_source_version='v2', model_version='model-A')
    assert result is not None
    assert storage_utils._load_people_embedding_index_npz('u1').source_version == 'v3'


@pytest.mark.parametrize('failure', ['data', 'manifest', 'cas'])
@pytest.mark.parametrize('strict', [True, False])
def test_publication_failure_never_reports_success_or_falls_back(ctx, monkeypatch, failure, strict):
    seed_base(ctx)
    _, _, blobs = ctx
    manifest_name = storage_utils._people_embedding_index_manifest_blob_name('u1')
    manifest_key = 'people-embedding-index/' + manifest_name
    before = blobs.blobs[manifest_key]
    original = _FakeBlob.upload_blob
    def upload(self, data, **kwargs):
        if kwargs.get('overwrite') is False:
            if failure == 'data':
                raise IOError('upload failed')
            if failure == 'cas':
                newer = json.loads(before)
                newer['sourceVersion'] = 'concurrent'
                blobs.blobs[manifest_key] = json.dumps(newer).encode()
        elif self._key == manifest_key and failure == 'manifest':
            raise IOError('manifest upload failed')
        return original(self, data, **kwargs)
    monkeypatch.setattr(_FakeBlob, 'upload_blob', upload)
    def no_fallback(*args):
        raise AssertionError('publication failure cannot trigger full fallback')
    monkeypatch.setattr(storage_utils, '_build_user_people_embedding_index_snapshot', no_fallback)
    result = storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v2', affected_person_ids=['p1'], incremental_only=strict,
        previous_source_version='v1', model_version='model-A')
    assert result is None
    assert storage_utils._load_people_embedding_index_npz('u1').source_version == 'v1'
    if failure != 'cas':
        assert blobs.blobs[manifest_key] == before


def test_input_and_membership_budgets_defer_before_publication(ctx, monkeypatch):
    seed_base(ctx)
    people, _, blobs = ctx
    before = dict(blobs.blobs)
    monkeypatch.setattr(incremental, 'MAX_IDS', 2)
    assert refresh(iter(['p1', 'p2', 'p3'])) is None
    monkeypatch.setattr(incremental, 'MAX_FACES', 1)
    people.rows[('u1', 'p1')]['faceIds'] = json.dumps(['f1', 'f1'])
    assert refresh(['p1']) is None
    assert blobs.blobs == before
    assert storage_utils.refresh_user_people_embedding_index('u1', incremental_only=True) is None


def test_delete_all_and_readd_from_empty_base(ctx):
    seed_base(ctx)
    people, faces, _ = ctx
    people.rows.clear()
    assert refresh(['p1', 'p2']).row_count == 0
    assert_equivalent()
    _seed_person(people, 'u1', 'p3', ['f3'], [1, 2])
    _seed_face(faces, 'u1', 'f3', 'p3')
    assert storage_utils.refresh_user_people_embedding_index(
        'u1', source_version='v3', affected_person_ids=['p3'], incremental_only=True,
        previous_source_version='v2', model_version='model-A').row_count == 1


def test_legacy_npz_without_model_provenance_defers(ctx):
    seed_base(ctx)
    assert storage_utils.refresh_user_people_embedding_index('u1', source_version='v1') is not None
    assert refresh(['p1']) is None


def test_stream_parser_cross_chunk_unicode_and_large_base(ctx, monkeypatch):
    seed_base(ctx)
    people, faces, _ = ctx
    for i in range(100):
        _seed_person(people, 'u1', f'p{i + 10}', [f'f{i + 10}'], [1, 2], name='名前🙂')
        _seed_face(faces, 'u1', f'f{i + 10}', f'p{i + 10}')
    storage_utils.refresh_user_people_embedding_index('u1', source_version='v1', model_version='model-A')
    monkeypatch.setattr(incremental, 'CHUNK', 13)
    assert refresh(['p2']) is not None
    assert_equivalent()


def test_transient_base_download_defers_without_publication(ctx, monkeypatch):
    seed_base(ctx)
    _, _, blobs = ctx
    before = dict(blobs.blobs)
    def fail(self, stream):
        stream.write(b'partial')
        raise TimeoutError('interrupted download')
    monkeypatch.setattr(_Downloaded, 'readinto', fail)
    assert refresh(['p1']) is None
    assert blobs.blobs == before


def test_missing_table_is_not_a_deleted_person(ctx, monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError
    seed_base(ctx)
    people, _, blobs = ctx
    before = dict(blobs.blobs)
    def fail(**kwargs):
        error = ResourceNotFoundError('table unavailable')
        error.error_code = 'TableNotFound'
        raise error
    monkeypatch.setattr(people, 'get_entity', fail)
    assert refresh(['p1']) is None
    assert blobs.blobs == before


@pytest.mark.parametrize('field,value', [('faceIds', 'invalid JSON'), ('repEmbedding', 'invalid JSON'),
                                        ('repEmbedding', '["bad", true, 2]')])
def test_malformed_json_and_numeric_filter_match_full_builder(ctx, field, value):
    seed_base(ctx)
    people, _, _ = ctx
    people.rows[('u1', 'p1')][field] = value
    assert refresh(['p1']) is not None
    assert_equivalent()


def test_scalar_ids_and_total_read_budget_defer(ctx, monkeypatch):
    seed_base(ctx)
    _, _, blobs = ctx
    before = dict(blobs.blobs)
    assert refresh('p1') is None
    monkeypatch.setattr(incremental, 'MAX_FACE_READS', 1)
    assert refresh(['p1', 'p2']) is None
    assert blobs.blobs == before


@pytest.mark.parametrize('failure', ['data', 'manifest'])
def test_full_refresh_also_requires_publication_success(ctx, monkeypatch, failure):
    seed_base(ctx)
    storage_utils.touch_user_people_embedding_index_state('u1')
    original = _FakeBlob.upload_blob
    def upload(self, data, **kwargs):
        if self._key.endswith('.npz' if failure == 'data' else '.json'):
            raise IOError('failed full publication')
        return original(self, data, **kwargs)
    monkeypatch.setattr(_FakeBlob, 'upload_blob', upload)
    assert storage_utils.refresh_user_people_embedding_index('u1', source_version='v2') is None
    assert storage_utils._load_people_embedding_index_manifest('u1')['dirty'] is True
    assert 'u1' not in storage_utils._PEOPLE_EMBEDDING_INDEX_CACHE

def test_parallel_face_failure_does_not_publish_partially_updated_people(ctx, monkeypatch):
    import threading
    seed_base(ctx)
    people, faces, blobs = ctx
    before = dict(blobs.blobs)
    original = faces.get_entity
    barrier = threading.Barrier(2, timeout=3)
    row = people.rows[('u1', 'p1')]
    row['faceIds'] = json.dumps(['f1', 'f-broken'])
    monkeypatch.setenv('INDEX_SOURCE_READ_CONCURRENCY', '2')

    def read(**kwargs):
        barrier.wait()
        if kwargs['row_key'] == 'f-broken':
            raise TimeoutError('second face read failed')
        return original(**kwargs)

    monkeypatch.setattr(faces, 'get_entity', read)
    assert refresh(['p1']) is None
    assert blobs.blobs == before
