"""The text vector built while processing a photo is off by default and can be switched back on."""
import pytest

import storage_utils
import vision_utils


@pytest.fixture
def encoder(monkeypatch):
    calls = []

    def encode(text):
        calls.append(text)
        return [0.5, 0.5]

    monkeypatch.setattr(vision_utils, 'encode_text_embedding', encode)
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'v1')
    monkeypatch.setattr(vision_utils, 'get_text_embedding_dimension', lambda: 2)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    return calls


def test_text_vector_is_off_by_default(encoder):
    assert storage_utils.SEMANTIC_TEXT_EMBEDDING_ENABLED is False


def test_disabled_skips_the_model_and_keeps_the_text(encoder, monkeypatch):
    monkeypatch.setattr(storage_utils, 'SEMANTIC_TEXT_EMBEDDING_ENABLED', False)
    row = {'PartitionKey': 'lib', 'semanticEmbedding': '[1]', 'semanticEmbeddingVersion': 'old', 'semanticTextDigest': 'x'}
    storage_utils._refresh_semantic_fields('beach.jpg', row)
    assert encoder == []
    assert 'semanticText' in row and 'semanticLayers' in row
    assert not {'semanticEmbedding', 'semanticEmbeddingVersion', 'semanticTextDigest'} & set(row)


def test_enabled_encodes_and_stores_the_vector(encoder, monkeypatch):
    monkeypatch.setattr(storage_utils, 'SEMANTIC_TEXT_EMBEDDING_ENABLED', True)
    row = {'PartitionKey': 'lib'}
    storage_utils._refresh_semantic_fields('beach.jpg', row)
    assert len(encoder) == 1
    assert row['semanticEmbedding'] == '[0.5,0.5]'
    assert row['semanticEmbeddingVersion'] == 'v1'
