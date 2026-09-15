"""Regression test for search_photos()'s color+object early-exit bug.

Previously `if has_context_intent and lexical_score <= 0: continue` (app.py)
dropped a row before its semantic/CLIP similarity was ever computed -- even
though every row's vector score was already precomputed in bulk just above.
A photo whose CLIP embedding matched "red car" well but whose stored tags
say "vehicle" (not "car") was filtered out with no chance for the semantic
signal to rescue it. Fixed by only filtering on the *combined* score, after
both lexical and semantic scores are computed.
"""
from __future__ import annotations

import app


def _row(filename: str, **overrides) -> dict:
    return {
        'RowKey': filename,
        'PartitionKey': 'owner',
        'tags': '[]',
        'objects': '[]',
        'peopleIds': '[]',
        'peopleNames': '[]',
        'exifData': '{}',
        'processing_metadata': '{}',
        **overrides,
    }


def test_color_object_query_surfaces_a_photo_found_only_via_semantic_score(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'person_table_client', None)
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: None)
    # Tags say "vehicle", not "car" -- lexical_search_score returns 0.0 for
    # this row's required_object=['car'] check. Only the precomputed vector
    # score (well above SEMANTIC_SEARCH_THRESHOLD) can rescue it.
    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', lambda *a, **k: [_row('vehicle.jpg', tags='["vehicle"]')])
    monkeypatch.setattr(app.vision_utils, 'encode_text_embedding', lambda text: [1.0, 0.0])
    monkeypatch.setattr(app, 'vector_search_candidates', lambda *a, **k: [('vehicle.jpg', 0.9)])

    with app.app.test_request_context('/photos/search?q=red car'):
        response = app.search_photos()

    payload = response.get_json() if hasattr(response, 'get_json') else response[0].get_json()
    filenames = [p['filename'] for p in payload['photos']]
    assert 'vehicle.jpg' in filenames
