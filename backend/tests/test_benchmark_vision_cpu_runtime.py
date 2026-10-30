"""Output checks for isolated model runtime experiments."""
import numpy as np
import pytest

from scripts.benchmark_vision_cpu_runtime import quality


def test_identical_vectors_have_identical_pairwise_neighbors():
    vectors = np.array([[1., 0.], [.8, .6], [0., 1.]])
    result = quality(vectors, vectors)
    assert result['min_cosine'] == pytest.approx(1)
    assert result['max_abs_error'] == result['max_pairwise_cosine_error'] == 0
    assert result['nearest_neighbor_changes'] == 0


@pytest.mark.parametrize('actual', [np.zeros((2, 3)), np.array([[np.nan, 0.]])])
def test_invalid_model_output_is_rejected(actual):
    with pytest.raises(ValueError):
        quality(actual, np.ones((1, 2)))


def test_quality_detects_changed_vectors_and_neighbors():
    original = np.array([[1., 0.], [.8, .6], [0., 1.]])
    actual = original[[2, 1, 0]]
    result = quality(actual, original)
    assert result['min_cosine'] == 0
    assert result['max_abs_error'] == 1
    assert result['nearest_neighbor_changes'] > 0
