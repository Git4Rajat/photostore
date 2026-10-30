"""Quantization reports must expose ranking and threshold changes."""
import numpy as np
import pytest
from scripts.benchmark_vision_int8 import vector_quality


def test_identical_vectors_have_no_quality_changes():
    vectors=np.array([[1.,0.],[.8,.6],[0.,1.]])
    result=vector_quality(vectors,vectors)
    assert result['min_cosine']==pytest.approx(1)
    assert result['nearest_neighbor_changes']==0
    assert result['max_pairwise_error']==0
    assert all(value==0 for value in result['threshold_pair_flips'].values())


def test_quantization_drift_is_not_hidden_by_average_cosine():
    reference=np.array([[1.,0.],[.8,.6],[0.,1.]])
    actual=reference[[2,1,0]]
    result=vector_quality(actual,reference)
    assert result['min_cosine']==0
    assert result['nearest_neighbor_changes']>0
    assert result['threshold_pair_flips']['0.7']>0


@pytest.mark.parametrize('actual',[np.ones((1,3)),np.array([[np.nan,0.]])])
def test_invalid_outputs_cannot_produce_a_quality_report(actual):
    with pytest.raises(ValueError):vector_quality(actual,np.ones((1,2)))
