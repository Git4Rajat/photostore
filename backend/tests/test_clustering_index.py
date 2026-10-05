"""Unit tests for clustering_index.py's FAISS index-building mechanics.

These test the index plumbing (build/add/search/serialize round-trip, flat
vs. IVFPQ selection, batching) with synthetic vectors -- correctness
properties that don't require real face embeddings. Candidate *recall*
against real data is a separate, explicitly out-of-scope-here concern (see
scripts/compare_faiss_candidates.py and the module's own docstring on why
this stays shadow-mode only).
"""
from __future__ import annotations

import numpy as np
import pytest

faiss = pytest.importorskip('faiss')

import clustering_index as ci


def _rows(vectors, tier='landmark-5pt', embedding_version='v1'):
    return [
        {'faceId': f'f{i}', 'embedding': list(vec), 'embeddingVersion': embedding_version}
        for i, vec in enumerate(vectors)
    ]


def test_build_face_index_returns_none_for_empty_input():
    assert ci.build_face_index('landmark-5pt', []) is None


def test_build_face_index_uses_flat_index_below_threshold():
    vectors = np.random.default_rng(0).normal(size=(20, 8)).astype('float32')
    build = ci.build_face_index('landmark-5pt', _rows(vectors))
    assert build.index_type == 'flat'
    assert build.vector_count == 20
    assert build.index.ntotal == 20
    assert build.dimension == 8
    assert build.embedding_version == 'v1'


def test_new_index_selects_ivfpq_above_threshold_and_disables_precomputed_tables():
    # Production-size construction; real train/add smoke coverage below uses
    # explicitly smaller codebooks and sufficient training examples.
    # 512 matches production's real ArcFace/AdaFace embedding dimension
    # (app.py's FACE_CLUSTER_EMBEDDING_DIMENSIONS) and divides evenly by the
    # default 64 subquantizers -- the common case this exercises.
    index, index_type = ci._new_index(dimension=512, vector_count=ci.FLAT_INDEX_MAX_VECTORS + 1)
    assert index_type == 'ivfpq'
    assert index.use_precomputed_table == -1
    assert 1 <= index.nlist <= (ci.FLAT_INDEX_MAX_VECTORS + 1) // 39


def test_new_index_falls_back_to_compatible_subquantizer_count():
    """A dimension not evenly divisible by IVFPQ_PQ_SUBQUANTIZERS must not
    crash -- fall back to the largest compatible divisor instead of letting
    faiss raise its opaque ProductQuantizer C++ error."""
    index, index_type = ci._new_index(dimension=17, vector_count=ci.FLAT_INDEX_MAX_VECTORS + 1)
    assert index_type == 'ivfpq'  # construction succeeded at all


def test_build_face_index_batches_without_losing_vectors():
    vectors = np.random.default_rng(2).normal(size=(37, 4)).astype('float32')
    build = ci.build_face_index('landmark-5pt', _rows(vectors), batch_size=10)
    assert build.vector_count == 37
    assert build.index.ntotal == 37
    assert len(build.face_ids) == 37
    assert build.face_ids == [f'f{i}' for i in range(37)]


def test_search_candidates_finds_nearest_neighbor_exactly():
    base = [1.0, 0.0, 0.0, 0.0]
    near = [0.99, 0.1, 0.0, 0.0]
    far = [0.0, 0.0, 1.0, 0.0]
    build = ci.build_face_index('landmark-5pt', _rows([base, near, far]))

    results = ci.search_candidates(build, base, k=3)

    assert results[0][0] == 'f0'
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)
    # near-identical vector ranks second, the orthogonal one last.
    result_ids = [r[0] for r in results]
    assert result_ids.index('f1') < result_ids.index('f2')


def test_search_candidates_returns_empty_for_empty_index():
    build = ci.build_face_index('landmark-5pt', [{'faceId': 'f0', 'embedding': [1.0, 0.0]}])
    build.index.reset()
    assert ci.search_candidates(build, [1.0, 0.0]) == []


def test_search_candidates_caps_k_to_index_size():
    vectors = [[1.0, 0.0], [0.0, 1.0]]
    build = ci.build_face_index('landmark-5pt', _rows(vectors))
    results = ci.search_candidates(build, [1.0, 0.0], k=50)
    assert len(results) == 2  # never more results than vectors in the index


def test_serialize_deserialize_round_trip_preserves_search_behavior():
    vectors = np.random.default_rng(3).normal(size=(30, 6)).astype('float32')
    build = ci.build_face_index('landmark-2pt', _rows(vectors, tier='landmark-2pt', embedding_version='v2'))

    index_bytes, manifest = ci.serialize_index(build)
    assert manifest['tier'] == 'landmark-2pt'
    assert manifest['embeddingVersion'] == 'v2'
    assert manifest['vectorCount'] == 30
    assert manifest['faceIds'] == build.face_ids

    restored = ci.deserialize_index(index_bytes, manifest)
    assert restored.index.ntotal == build.index.ntotal
    assert restored.face_ids == build.face_ids

    query = list(vectors[5])
    original_results = ci.search_candidates(build, query, k=1)
    restored_results = ci.search_candidates(restored, query, k=1)
    assert original_results == restored_results


@pytest.mark.parametrize('factory', [False, True])
def test_streaming_generator_and_factory(factory):
    rows = _rows([[1, 0], [0, 1], [1, 1]])
    source = (lambda: iter(rows)) if factory else iter(rows)
    build = ci.build_face_index('tier', source, vector_count=3, batch_size=1)
    assert build.face_ids == ['f0', 'f1', 'f2']
    assert ci.build_face_index('tier', iter(())) is None


def test_input_count_checked_including_invalid_rows():
    rows = _rows([[1, 0], [0, 0]])
    assert ci.build_face_index('tier', iter(rows), vector_count=2).vector_count == 1
    with pytest.raises(ValueError, match='count'):
        ci.build_face_index('tier', iter(rows), vector_count=3)


@pytest.mark.parametrize('bad', [[0, 0], [float('nan'), 1], [float('inf'), 1], [], 'not json', [[1, 2]]])
def test_invalid_rows_and_queries_skipped(bad):
    rows = [{'faceId': 'bad', 'embedding': bad}, {'faceId': 'ok', 'embedding': '[1, 0]'}]
    build = ci.build_face_index('tier', iter(rows))
    assert build.face_ids == ['ok']
    assert ci.search_candidates(build, bad) == []
    assert ci.search_candidates(build, [1, 0, 0]) == []
    assert ci.search_candidates(build, [1, 0], k=0) == []


@pytest.mark.parametrize('change', [
    {'embedding': [1, 0, 0]}, {'embeddingVersion': 'v2'}, {'tier': 'other'}, {'faceId': 'f0'},
])
def test_mixed_compatibility_or_duplicate_ids_fail_closed(change):
    rows = _rows([[1, 0], [0, 1]])
    rows[1].update(change)
    with pytest.raises(ValueError):
        ci.build_face_index('tier', iter(rows))


def test_training_sample_is_deterministic_and_covers_late_rows(monkeypatch):
    from types import SimpleNamespace
    samples = []
    def new_index(dimension, count, **kwargs):
        return SimpleNamespace(
            train=lambda vectors: samples.append(vectors.copy()),
            add=lambda vectors: None, nlist=4), 'ivfpq'
    monkeypatch.setattr(ci, '_new_index', new_index)
    config = ci.IndexConfig(training_sample_size=300, seed=42)
    for _ in range(2):
        rows = ({'faceId': str(i), 'embedding': [1, i / 1000]} for i in range(5000))
        build = ci.build_face_index('tier', rows, config=config, batch_size=17)
        assert build.vector_count == 5000
    assert samples[0].shape == (300, 2)
    np.testing.assert_array_equal(samples[0], samples[1])
    assert samples[0][:, 1].max() > 0.95  # well beyond first 2,000 rows


def test_real_ivfpq_streaming_train_add_roundtrip():
    config = ci.IndexConfig(flat_max_vectors=10, nlist_min=4, nlist_max=4,
                            pq_subquantizers=2, pq_bits=4, training_sample_size=624,
                            training_iterations=2)
    threads = faiss.omp_get_max_threads()
    try:
        faiss.omp_set_num_threads(1)
        rng = np.random.default_rng(13)
        source = ({'faceId': str(i), 'embedding': rng.normal(size=8), 'embeddingVersion': 'v1'}
                  for i in range(700))
        build = ci.build_face_index('tier', source, vector_count=700, batch_size=23, config=config)
        assert build.index_type == 'ivfpq'
        assert build.index.is_trained and build.index.ntotal == 700
        assert build.index.nlist == 4
        assert build.index.use_precomputed_table == -1
        restored = ci.deserialize_index(*ci.serialize_index(build))
        assert restored.index.use_precomputed_table == -1
        assert ci.search_candidates(restored, [1] * 8, k=5)
    finally:
        faiss.omp_set_num_threads(threads)


def test_adaptive_nlist_and_insufficient_pq_training():
    config = ci.IndexConfig(flat_max_vectors=0, training_sample_size=300, pq_bits=2)
    index, kind = ci._new_index(8, 12000, training_count=300, config=config)
    assert kind == 'ivfpq' and index.nlist == 300 // 39
    with pytest.raises(ValueError, match='Insufficient'):
        ci.build_face_index('tier', iter(_rows([[1, 0]] * 20)), config=config)


def test_memory_budget_rejects_oversized_batch():
    config = ci.IndexConfig(memory_budget_bytes=1024)
    with pytest.raises(ValueError, match='memory budget'):
        ci.build_face_index('tier', iter(_rows([[1, 0]])), config=config)


@pytest.mark.parametrize('change', [
    {'vectorCount': 3}, {'dimension': 3}, {'vectorCount': '2'}, {'faceIds': ['f0']},
    {'faceIds': ['f0', 'f0']}, {'tier': None}, {'embeddingVersion': None},
    {'indexType': 'ivfpq'}, {'indexType': 'unknown'},
])
def test_deserialize_rejects_invalid_or_mismatching_manifest(change):
    build = ci.build_face_index('tier', _rows([[1, 0], [0, 1]]))
    blob, manifest = ci.serialize_index(build)
    manifest.update(change)
    with pytest.raises(ValueError):
        ci.deserialize_index(blob, manifest)


def test_spool_closed_when_input_generator_fails(monkeypatch):
    original = ci.tempfile.TemporaryFile
    files = []
    def temporary_file():
        result = original()
        files.append(result)
        return result
    monkeypatch.setattr(ci.tempfile, 'TemporaryFile', temporary_file)
    def source():
        yield {'faceId': 'a', 'embedding': [1, 0]}
        raise RuntimeError('input failed')
    with pytest.raises(RuntimeError, match='input failed'):
        ci.build_face_index('tier', source())
    assert files and all(file.closed for file in files)


def test_deserialize_rejects_corrupt_bytes():
    build = ci.build_face_index('tier', _rows([[1, 0]]))
    _, manifest = ci.serialize_index(build)
    with pytest.raises(ValueError, match='serialized'):
        ci.deserialize_index(b'invalid', manifest)
