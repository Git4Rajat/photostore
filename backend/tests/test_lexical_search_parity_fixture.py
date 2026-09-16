"""Guards tests/fixtures/lexical_search_parity.json against silent drift.

This fixture is the shared contract with the TypeScript port of lexical
search (frontend/src/services/localLexicalSearch.ts, used for client-side
/photos/search -- see backend-cpu-optimization-2026-09 memory). If this test
fails, search_utils.py's scoring changed without regenerating the fixture:
run `python scripts/generate_lexical_search_parity_fixtures.py`, then update
localLexicalSearch.ts to match the new behavior before committing either.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import app as app_module
import search_utils

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'lexical_search_parity.json')


def _load_cases():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)


CASES = _load_cases()


def test_fixture_file_is_nonempty():
    assert len(CASES) >= 10


import pytest  # noqa: E402


@pytest.mark.parametrize('case', CASES, ids=[c['name'] for c in CASES])
def test_case_matches_live_search_utils_output(case):
    row = case['row']
    filename = case['filename']
    query = case['query']
    name_to_ids = case['nameToIds']
    capture_start = _parse_date(case['captureStart'])
    capture_end = _parse_date(case['captureEnd'])
    expected = case['expected']

    tokens = search_utils.parse_search_query(query)
    assert tokens['subject'] == expected['tokens']['subject']
    assert tokens['location'] == expected['tokens']['location']
    assert tokens['all'] == expected['tokens']['all']
    assert tokens['expanded'] == expected['tokens']['expanded']
    assert tokens['modifiers'] == expected['tokens']['modifiers']
    assert tokens['required_object'] == expected['tokens']['requiredObject']
    assert tokens['exact_phrases'] == expected['tokens']['exactPhrases']

    exif_data = search_utils.parse_exif_data(row.get('exifData', '{}'))
    matched_person_groups = app_module._matched_query_people_groups(query, name_to_ids)
    matched_location_terms = app_module._matched_query_locations(query, [row])
    assert matched_person_groups == expected['matchedPersonGroups']
    assert matched_location_terms == expected['matchedLocationTerms']

    passes_filters = app_module._row_passes_search_filters(
        row, capture_start, capture_end, matched_person_groups, matched_location_terms,
    )
    assert passes_filters == expected['passesFilters']

    lexical_score = search_utils.lexical_search_score(tokens, filename, row, exif_data)
    assert round(lexical_score, 6) == expected['lexicalScore']

    combined_score = lexical_score
    if matched_person_groups:
        combined_score += 8.0 * len(matched_person_groups)
    if matched_location_terms:
        combined_score += 5.0
    assert round(combined_score, 6) == expected['combinedScore']

    query_embedding = case.get('queryEmbedding')
    row_embedding = case.get('rowEmbedding')
    if query_embedding is not None and row_embedding is not None:
        semantic_score = search_utils.cosine_similarity(query_embedding, row_embedding)
        assert round(semantic_score, 6) == expected['semanticScore']
        combined_with_semantic = lexical_score
        if semantic_score >= 0.16:
            combined_with_semantic += semantic_score * 10.0
        if matched_person_groups:
            combined_with_semantic += 8.0 * len(matched_person_groups)
        if matched_location_terms:
            combined_with_semantic += 5.0
        assert round(combined_with_semantic, 6) == expected['combinedScoreWithSemantic']
