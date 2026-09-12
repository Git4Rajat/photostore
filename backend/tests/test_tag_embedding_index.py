"""Tests for the per-user tag-embedding index (storage_utils.py) and its
query-time consumer (app._expand_tokens_with_tag_embeddings).

Lets search expand an out-of-vocabulary query word ("puppy") to whichever of
a user's *actual* tags are nearest to it in CLIP embedding space ("dog").
Real CLIP text embeddings only exist where torch/open_clip are installed --
ipworker, never the plain backend role that serves /photos/search (see
docs/ipworker-architecture.md) -- so the expensive build step must refuse to
run anywhere else, and the query-time lookup must be pure numpy against two
precomputed caches (a static common-word vocabulary table and this per-user
tag-embedding cache).
"""
from __future__ import annotations

import numpy as np
import pytest

import app
import storage_utils
import vision_utils
from tests.fakes import FakeTable


@pytest.fixture
def tag_embedding_ctx(monkeypatch):
    table = FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_tag_embedding_index_container', '')
    storage_utils._TAG_EMBEDDING_INDEX_CACHE.clear()
    yield table
    storage_utils._TAG_EMBEDDING_INDEX_CACHE.clear()


def _unit(vector):
    arr = np.asarray(vector, dtype=np.float32)
    return arr / np.linalg.norm(arr)


# --- _build_user_tag_embedding_index_snapshot -------------------------------

def test_build_snapshot_refuses_to_run_without_real_clip(monkeypatch, tag_embedding_ctx):
    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: False)
    snapshot = storage_utils._build_user_tag_embedding_index_snapshot('lib-A', 'v1')
    assert snapshot is None


def test_build_snapshot_embeds_distinct_effective_tags(monkeypatch, tag_embedding_ctx):
    table = tag_embedding_ctx
    table.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': 'a.jpg', 'tags': '["dog"]', 'subjectTags': '[]'})
    table.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': 'b.jpg', 'tags': '["dog", "beach"]', 'subjectTags': '[]'})

    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'clip-test-v1')
    monkeypatch.setattr(
        vision_utils, 'encode_text_embeddings_batch',
        lambda texts: [[1.0, 0.0] if t == 'beach' else [0.0, 1.0] for t in texts],
    )

    snapshot = storage_utils._build_user_tag_embedding_index_snapshot('lib-A', 'v1')

    assert snapshot is not None
    assert snapshot.tags == ['beach', 'dog']
    assert snapshot.embeddings.shape == (2, 2)


def test_build_snapshot_returns_none_when_embedding_call_fails(monkeypatch, tag_embedding_ctx):
    table = tag_embedding_ctx
    table.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': 'a.jpg', 'tags': '["dog"]', 'subjectTags': '[]'})
    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(vision_utils, 'encode_text_embeddings_batch', lambda texts: [])

    snapshot = storage_utils._build_user_tag_embedding_index_snapshot('lib-A', 'v1')

    assert snapshot is None


# --- nearest_tags_for_word ---------------------------------------------------

def test_nearest_tags_for_word_returns_matches_above_threshold():
    tag_index = {
        'tags': ['dog', 'beach'],
        'embeddings': np.vstack([_unit([1.0, 0.01]), _unit([0.0, 1.0])]),
    }
    close_to_dog = _unit([1.0, 0.0]).tolist()

    result = storage_utils.nearest_tags_for_word(close_to_dog, tag_index, top_k=3, min_similarity=0.9)

    assert result == ['dog']


def test_nearest_tags_for_word_empty_when_nothing_clears_threshold():
    tag_index = {'tags': ['beach'], 'embeddings': np.vstack([_unit([0.0, 1.0])])}
    unrelated = _unit([1.0, 0.0]).tolist()

    assert storage_utils.nearest_tags_for_word(unrelated, tag_index, min_similarity=0.5) == []


def test_nearest_tags_for_word_handles_missing_or_malformed_input():
    assert storage_utils.nearest_tags_for_word([], {'tags': ['dog'], 'embeddings': np.zeros((1, 2))}) == []
    assert storage_utils.nearest_tags_for_word([1.0, 0.0], {}, ) == []
    assert storage_utils.nearest_tags_for_word([1.0, 0.0, 0.0], {'tags': ['dog'], 'embeddings': np.zeros((1, 2))}) == []


# --- touch_user_search_indexes_state -----------------------------------------

def test_touch_user_search_indexes_state_touches_all_three_indexes(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_vector_index_state', lambda uid, embedding_version=None: calls.append('vector'))
    monkeypatch.setattr(storage_utils, 'touch_user_lexical_index_state', lambda uid: calls.append('lexical'))
    monkeypatch.setattr(storage_utils, 'touch_user_tag_embedding_index_state', lambda uid: calls.append('tag'))

    storage_utils.touch_user_search_indexes_state('lib-A')

    assert calls == ['vector', 'lexical', 'tag']


# --- app._expand_tokens_with_tag_embeddings ----------------------------------

def test_expand_tokens_adds_nearest_real_tag_for_unknown_word(monkeypatch):
    monkeypatch.setattr(app, 'get_user_tag_embedding_index', lambda uid, allow_refresh=False: {
        'tags': ['dog', 'beach'],
        'embeddings': np.vstack([_unit([1.0, 0.01]), _unit([0.0, 1.0])]),
    })
    monkeypatch.setattr(app.vision_utils, 'common_word_embedding', lambda word: _unit([1.0, 0.0]).tolist() if word == 'puppy' else [])

    tokens = {'all': ['puppy'], 'expanded': []}
    app._expand_tokens_with_tag_embeddings(tokens, 'lib-A')

    assert tokens['expanded'] == ['dog']


def test_expand_tokens_skips_words_already_an_exact_tag_match(monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'get_user_tag_embedding_index', lambda uid, allow_refresh=False: {
        'tags': ['dog'],
        'embeddings': np.vstack([_unit([1.0, 0.0])]),
    })
    monkeypatch.setattr(app.vision_utils, 'common_word_embedding', lambda word: calls.append(word) or [])

    tokens = {'all': ['dog'], 'expanded': []}
    app._expand_tokens_with_tag_embeddings(tokens, 'lib-A')

    assert tokens['expanded'] == []
    assert calls == []  # never even looked up -- already an exact match


def test_expand_tokens_noop_for_word_outside_static_vocabulary(monkeypatch):
    monkeypatch.setattr(app, 'get_user_tag_embedding_index', lambda uid, allow_refresh=False: {
        'tags': ['dog'],
        'embeddings': np.vstack([_unit([1.0, 0.0])]),
    })
    monkeypatch.setattr(app.vision_utils, 'common_word_embedding', lambda word: [])

    tokens = {'all': ['zzzznonword'], 'expanded': []}
    app._expand_tokens_with_tag_embeddings(tokens, 'lib-A')

    assert tokens['expanded'] == []


def test_expand_tokens_noop_when_no_tag_index_available(monkeypatch):
    monkeypatch.setattr(app, 'get_user_tag_embedding_index', lambda uid, allow_refresh=False: None)

    tokens = {'all': ['puppy'], 'expanded': []}
    app._expand_tokens_with_tag_embeddings(tokens, 'lib-A')

    assert tokens['expanded'] == []


# --- End-to-end through search_photos() --------------------------------------

def _row(filename: str, **overrides) -> dict:
    return {
        'RowKey': filename, 'PartitionKey': 'owner', 'tags': '[]', 'objects': '[]',
        'peopleIds': '[]', 'peopleNames': '[]', 'exifData': '{}', 'processing_metadata': '{}',
        **overrides,
    }


def test_search_photos_surfaces_photo_via_tag_embedding_expansion(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'person_table_client', None)
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: None)
    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', lambda *a, **k: [_row('dog.jpg', tags='["dog"]')])
    monkeypatch.setattr(app.vision_utils, 'encode_text_embedding', lambda text: [])
    monkeypatch.setattr(app, 'vector_search_candidates', lambda *a, **k: [])
    monkeypatch.setattr(app, 'get_user_tag_embedding_index', lambda uid, allow_refresh=False: {
        'tags': ['dog'],
        'embeddings': np.vstack([_unit([1.0, 0.0])]),
    })
    monkeypatch.setattr(app.vision_utils, 'common_word_embedding', lambda word: _unit([1.0, 0.01]).tolist() if word == 'puppy' else [])

    with app.app.test_request_context('/photos/search?q=puppy'):
        response = app.search_photos()

    payload = response.get_json() if hasattr(response, 'get_json') else response[0].get_json()
    filenames = [p['filename'] for p in payload['photos']]
    assert 'dog.jpg' in filenames
