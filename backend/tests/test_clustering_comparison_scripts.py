"""Offline script regressions: fake app only, no storage initialization."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import clustering_index as ci


def _script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.app is None
    return module


@pytest.mark.parametrize('initial', [True, False])
@pytest.mark.parametrize('failure', [True, False])
def test_full_reference_disables_bounding_and_restores(initial, failure):
    script = _script('compare_bounded_exemplars')
    def compute(*args):
        assert script.app.PEOPLE_REP_BOUNDED_EXEMPLARS is False
        if failure:
            raise RuntimeError('reference failed')
        return [1, 0]
    script.app = SimpleNamespace(PEOPLE_REP_BOUNDED_EXEMPLARS=initial,
                                 _compute_rep_embedding_for_face_ids=compute)
    if failure:
        with pytest.raises(RuntimeError):
            script._full_rep('u', ['f1', 'f2'], 'p', {})
    else:
        assert script._full_rep('u', ['f1', 'f2'], 'p', {}) == [1, 0]
    assert script.app.PEOPLE_REP_BOUNDED_EXEMPLARS is initial


def test_streaming_reps_skip_mixed_unknown_and_versions():
    script = _script('compare_faiss_candidates')
    summary = {
        'a': {'tier': '5pt', 'version': 'v1'},
        'b': {'tier': '2pt', 'version': 'v1'},
        'c': {'tier': '5pt', 'version': 'v2'},
    }
    people = [
        {'RowKey': 'good', 'faceIds': '["a"]', 'repEmbedding': '[1, 0]'},
        {'RowKey': 'mixed-tier', 'faceIds': '["a", "b"]', 'repEmbedding': '[1, 0]'},
        {'RowKey': 'mixed-version', 'faceIds': '["a", "c"]', 'repEmbedding': '[1, 0]'},
        {'RowKey': 'unknown', 'faceIds': '["a", "missing"]', 'repEmbedding': '[1, 0]'},
    ]
    script.app = SimpleNamespace(
        _load_user_face_summary_by_id=lambda user: summary,
        person_table_client=SimpleNamespace(query_entities=lambda *args, **kw: iter(people)),
        _escape_odata=lambda user: user,
        _face_is_clusterable=lambda face: True,
        _face_embedding_allowed_for_clustering=lambda face: True,
        _face_alignment_tier=lambda face: face['tier'],
        _face_embedding_version=lambda face: face['version'],
    )
    rows = list(script._person_rep_rows_by_tier('u'))
    assert len(rows) == 1
    assert rows[0][0] == ('5pt', 2, 'v1')
    assert rows[0][1]['faceId'] == 'good'


def test_comparison_same_population_top_two_and_exact_rerank(monkeypatch):
    pytest.importorskip('faiss')
    script = _script('compare_faiss_candidates')
    key = ('5pt', 2, 'v1')
    rows = ((key, {'faceId': pid, 'embedding': vector, 'embeddingVersion': 'v1'})
            for pid, vector in [('best', [1, 0]), ('runner', [.9, .1]), ('far', [0, 1])])
    pairs = script._build_comparison_indexes(rows)
    approximate, exact = pairs[key]
    assert approximate.face_ids == exact.face_ids
    query = ci.normalized_vector('[1, 0]')
    all_candidates = script._compare_query(approximate, exact, query, 3, .8, .1)
    assert all(all_candidates.values())
    original = ci.search_candidates
    def missed_runner(build, vector, k=10):
        if build is approximate:
            return [('far', .99), ('best', .2)]  # wrong ANN scores/order
        return original(build, vector, k)
    monkeypatch.setattr(ci, 'search_candidates', missed_runner)
    metrics = script._compare_query(approximate, exact, query, 2, .8, .1)
    assert metrics == {'top1_agree': False, 'best_recall': True, 'top_two_recall': False,
                       'reranked_top1_agree': True, 'decision_agree': False}


def test_baseline_separates_tiers_dimensions_versions():
    pytest.importorskip('faiss')
    script = _script('compare_faiss_candidates')
    keys = [('5pt', 2, 'v1'), ('2pt', 2, 'v1'), ('5pt', 3, 'v1'), ('5pt', 2, 'v2')]
    rows = ((key, {'faceId': str(i), 'embedding': [1] + [0] * (key[1] - 1),
                   'embeddingVersion': key[2]}) for i, key in enumerate(keys))
    builds = script._build_comparison_indexes(rows)
    assert set(builds) == set(keys)
    for i, key in enumerate(keys):
        assert builds[key][1].face_ids == [str(i)]


def test_query_sampling_is_bounded_deterministic_reservoir():
    import random
    script = _script('compare_faiss_candidates')
    script.app = SimpleNamespace(
        _load_user_face_summary_by_id=lambda user: {str(i): {} for i in range(1000)},
        _face_is_clusterable=lambda row: True,
        _face_embedding_allowed_for_clustering=lambda row: True,
    )
    first = script._sample_query_faces('u', 20, random.Random(0))
    assert first == script._sample_query_faces('u', 20, random.Random(0))
    assert len(first) == 20 and max(int(fid) for fid, _ in first) > 500