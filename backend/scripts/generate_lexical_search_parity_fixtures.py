"""Regenerates tests/fixtures/lexical_search_parity.json: a set of
(row, query, ...) inputs run through the REAL search_utils.py / app.py
lexical-matching functions to produce "golden" expected outputs.

This fixture is the shared contract between the Python implementation and
its TypeScript port (frontend/src/services/localLexicalSearch.ts, which
mirrors it for client-side search -- see backend-cpu-optimization-2026-09
memory). Two tests guard it from opposite directions:
  - backend/tests/test_lexical_search_parity_fixture.py fails if live
    search_utils.py output no longer matches the committed fixture (i.e.
    someone changed the scoring and forgot this exists).
  - frontend's localLexicalSearch.parity.test.ts fails if the TS port no
    longer matches the same fixture.

Run this script and commit the regenerated fixture (after also updating the
TS port to match) whenever search_utils.py's lexical scoring changes.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import search_utils  # noqa: E402
import app as app_module  # noqa: E402


def _row(**overrides):
    base = {
        'RowKey': overrides.get('filename', 'photo.jpg'),
        'PartitionKey': 'owner',
        'tags': '[]',
        'subjectTags': '[]',
        'backgroundTags': '[]',
        'objects': '[]',
        'peopleNames': '[]',
        'peopleIds': '[]',
        'locationCity': '',
        'locationRegion': '',
        'locationCountry': '',
        'address': '',
        'exifData': '{}',
        'faceCount': 0,
        'ocrText': '',
        'caption': '',
    }
    base.update(overrides)
    return base


CASES = [
    {
        'name': 'simple_tag_match',
        'filename': 'dog1.jpg',
        'row': _row(filename='dog1.jpg', tags='["dog", "park"]', subjectTags='["dog"]'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'plural_stemming_match',
        'filename': 'dog1.jpg',
        'row': _row(filename='dog1.jpg', tags='["dog"]', subjectTags='["dog"]'),
        'query': 'dogs',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'visual_modifier_object_tag_match',
        'filename': 'car1.jpg',
        'row': _row(filename='car1.jpg', tags='["car", "red"]', subjectTags='["car"]'),
        'query': 'red car',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'visual_modifier_object_no_modifier_match',
        'filename': 'car2.jpg',
        'row': _row(filename='car2.jpg', tags='["car"]', subjectTags='["car"]'),
        'query': 'red car',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'visual_modifier_object_no_match_at_all',
        'filename': 'boat1.jpg',
        'row': _row(filename='boat1.jpg', tags='["boat"]', subjectTags='["boat"]'),
        'query': 'red car',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'location_query_match',
        'filename': 'trip1.jpg',
        'row': _row(filename='trip1.jpg', locationCity='Malmo', locationCountry='Sweden'),
        'query': 'malmo',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'subject_in_location_combo',
        'filename': 'trip2.jpg',
        'row': _row(filename='trip2.jpg', tags='["dog"]', subjectTags='["dog"]', locationCity='Malmo'),
        'query': 'dog in malmo',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'people_single_match',
        'filename': 'alice1.jpg',
        'row': _row(filename='alice1.jpg', peopleIds='["p1"]'),
        'query': 'alice',
        'name_to_ids': {'alice': ['p1']},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'people_multi_match_both_required',
        'filename': 'both.jpg',
        'row': _row(filename='both.jpg', peopleIds='["p1", "p2"]'),
        'query': 'alice and bob',
        'name_to_ids': {'alice': ['p1'], 'bob': ['p2']},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'people_multi_match_only_one_present',
        'filename': 'onlyalice.jpg',
        'row': _row(filename='onlyalice.jpg', peopleIds='["p1"]'),
        'query': 'alice and bob',
        'name_to_ids': {'alice': ['p1'], 'bob': ['p2']},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'synonym_expansion_match',
        'filename': 'fall1.jpg',
        'row': _row(filename='fall1.jpg', tags='["waterfall"]', subjectTags='["waterfall"]'),
        'query': 'water',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'capture_date_in_range',
        'filename': 'dated1.jpg',
        'row': _row(filename='dated1.jpg', tags='["dog"]', subjectTags='["dog"]', exifData='{"DateTimeOriginal": "2026:06:15 10:00:00"}'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': '2026-06-01',
        'capture_end': '2026-06-30',
    },
    {
        'name': 'capture_date_out_of_range',
        'filename': 'dated2.jpg',
        'row': _row(filename='dated2.jpg', tags='["dog"]', subjectTags='["dog"]', exifData='{"DateTimeOriginal": "2026:07:15 10:00:00"}'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': '2026-06-01',
        'capture_end': '2026-06-30',
    },
    {
        'name': 'no_match_returns_zero',
        'filename': 'unrelated.jpg',
        'row': _row(filename='unrelated.jpg', tags='["cat"]', subjectTags='["cat"]'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'filename_only_match',
        'filename': 'sunset_beach_photo.jpg',
        'row': _row(filename='sunset_beach_photo.jpg'),
        'query': 'sunset',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    {
        'name': 'ocr_text_match',
        'filename': 'sign1.jpg',
        'row': _row(filename='sign1.jpg', ocrText='Welcome to Springfield'),
        'query': 'springfield',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
    },
    # --- Semantic blend cases (app._score_search_row's non-lexical half) ---
    # Small synthetic embeddings (not real CLIP output) -- these exist to pin
    # the BLEND ARITHMETIC (threshold gate, *10.0 weight, stacking with the
    # lexical/person/location terms) in a language-independent, ML-free way,
    # not to validate real CLIP semantics (see localVectorIndexParser's own
    # fixture/tests for the real-embedding-format concern).
    {
        'name': 'semantic_above_threshold_no_lexical_match',
        'filename': 'semantic1.jpg',
        'row': _row(filename='semantic1.jpg', tags='["cat"]', subjectTags='["cat"]'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
        'query_embedding': [1.0, 0.0, 0.0, 0.0],
        'row_embedding': [1.0, 0.0, 0.0, 0.0],
    },
    {
        'name': 'semantic_below_threshold_no_lexical_match',
        'filename': 'semantic2.jpg',
        'row': _row(filename='semantic2.jpg', tags='["cat"]', subjectTags='["cat"]'),
        'query': 'dog',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
        'query_embedding': [1.0, 0.0, 0.0, 0.0],
        'row_embedding': [0.0, 1.0, 0.0, 0.0],
    },
    {
        'name': 'semantic_plus_lexical_plus_location_all_stack',
        'filename': 'semantic3.jpg',
        'row': _row(filename='semantic3.jpg', tags='["dog"]', subjectTags='["dog"]', locationCity='Malmo'),
        'query': 'dog in malmo',
        'name_to_ids': {},
        'capture_start': None,
        'capture_end': None,
        'query_embedding': [0.6, 0.8, 0.0, 0.0],
        'row_embedding': [0.8, 0.6, 0.0, 0.0],
    },
]


def _parse_date(value):
    if not value:
        return None
    from datetime import datetime, timezone
    return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)


def build_fixture():
    results = []
    for case in CASES:
        row = case['row']
        filename = case['filename']
        query = case['query']
        name_to_ids = case['name_to_ids']
        capture_start = _parse_date(case['capture_start'])
        capture_end = _parse_date(case['capture_end'])

        tokens = search_utils.parse_search_query(query)
        exif_data = search_utils.parse_exif_data(row.get('exifData', '{}'))
        matched_person_groups = app_module._matched_query_people_groups(query, name_to_ids)
        matched_location_terms = app_module._matched_query_locations(query, [row])
        passes_filters = app_module._row_passes_search_filters(
            row, capture_start, capture_end, matched_person_groups, matched_location_terms,
        )
        lexical_score = search_utils.lexical_search_score(tokens, filename, row, exif_data)
        combined_score = lexical_score
        if matched_person_groups:
            combined_score += 8.0 * len(matched_person_groups)
        if matched_location_terms:
            combined_score += 5.0

        semantic_score = None
        combined_score_with_semantic = None
        query_embedding = case.get('query_embedding')
        row_embedding = case.get('row_embedding')
        if query_embedding is not None and row_embedding is not None:
            semantic_score = search_utils.cosine_similarity(query_embedding, row_embedding)
            combined_score_with_semantic = lexical_score
            semantic_threshold = 0.16  # SEMANTIC_SEARCH_THRESHOLD's documented default
            if semantic_score >= semantic_threshold:
                combined_score_with_semantic += semantic_score * 10.0
            if matched_person_groups:
                combined_score_with_semantic += 8.0 * len(matched_person_groups)
            if matched_location_terms:
                combined_score_with_semantic += 5.0

        results.append({
            'name': case['name'],
            'filename': filename,
            'row': row,
            'query': query,
            'nameToIds': name_to_ids,
            'captureStart': case['capture_start'],
            'captureEnd': case['capture_end'],
            'queryEmbedding': query_embedding,
            'rowEmbedding': row_embedding,
            'expected': {
                'tokens': {
                    'subject': tokens['subject'],
                    'location': tokens['location'],
                    'all': tokens['all'],
                    'expanded': tokens['expanded'],
                    'modifiers': tokens['modifiers'],
                    'requiredObject': tokens['required_object'],
                    'exactPhrases': tokens['exact_phrases'],
                },
                'matchedPersonGroups': matched_person_groups,
                'matchedLocationTerms': matched_location_terms,
                'passesFilters': passes_filters,
                'lexicalScore': round(lexical_score, 6),
                'combinedScore': round(combined_score, 6),
                'semanticScore': round(semantic_score, 6) if semantic_score is not None else None,
                'combinedScoreWithSemantic': round(combined_score_with_semantic, 6) if combined_score_with_semantic is not None else None,
            },
        })
    return results


if __name__ == '__main__':
    fixture = build_fixture()
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tests', 'fixtures', 'lexical_search_parity.json')
    out_path = os.path.normpath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(fixture, f, indent=2, sort_keys=True)
        f.write('\n')
    print(f'Wrote {len(fixture)} cases to {out_path}')
