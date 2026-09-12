"""Regression tests for search_utils.py's query parsing and scoring.

Covers the bugs found and fixed during the 2026-09-12 search-gap audit
(comparing against how Apple Photos search works): OCR text was excluded
from the searchable semantic text by an allowlist filter; location queries
with filler words ("the", "a") zeroed the whole score via an AND-across-
all-location-tokens check; a handful of already-singular nouns ending in a
lone "s" (lens, canvas, focus, ...) were mis-stemmed into a different token
than their own correctly-reduced plural; and the modifier vocabulary only
recognized colors, not size/age/material adjectives.
"""
from __future__ import annotations

from search_utils import (
    build_semantic_text,
    lexical_search_score,
    parse_search_query,
    _normalize_token,
)


def test_ocr_text_is_searchable_even_when_not_a_known_tag():
    metadata = {
        'subjectTags': '[]',
        'ocrText': 'INCOME TAX DEPARTMENT GOVT. OF INDIA\nPermanent Account Number Card\nAAAAA0000A',
    }
    text = build_semantic_text('IMG_0365.heic', metadata)
    assert 'income' in text
    assert 'tax' in text

    for query in ('income', 'tax', 'income tax'):
        tokens = parse_search_query(query)
        assert lexical_search_score(tokens, 'IMG_0365.heic', metadata, {}) > 0


def test_location_query_with_filler_word_matches_same_as_without():
    metadata = {'tags': '["dog", "beach"]', 'subjectTags': '[]'}
    with_filler = lexical_search_score(parse_search_query('dog at the beach'), 'photo.jpg', metadata, {})
    without_filler = lexical_search_score(parse_search_query('dog at beach'), 'photo.jpg', metadata, {})
    assert with_filler == without_filler
    assert with_filler > 0


def test_region_only_location_match():
    metadata = {
        'tags': '["beach"]', 'subjectTags': '[]',
        'locationCity': 'Palo Alto', 'locationRegion': 'California', 'locationCountry': 'United States',
    }
    tokens = parse_search_query('beach in california')
    assert lexical_search_score(tokens, 'photo.jpg', metadata, {}) > 0


def test_singular_and_plural_forms_of_lens_converge():
    assert _normalize_token('lens') == _normalize_token('lenses') == 'lens'


def test_singular_nouns_ending_in_s_are_not_mangled():
    for word in ('canvas', 'focus', 'campus', 'circus', 'virus', 'octopus'):
        assert _normalize_token(word) == word


def test_modifier_vocabulary_includes_size_age_and_material():
    for modifier, obj in (('big', 'dog'), ('old', 'car'), ('wooden', 'table')):
        tokens = parse_search_query(f'{modifier} {obj}')
        assert tokens['modifiers'] == [modifier]
        assert tokens['required_object'] == [obj]


def test_color_object_query_still_scores_via_tags():
    metadata = {'tags': '["red", "car"]', 'subjectTags': '[]'}
    tokens = parse_search_query('red car')
    assert lexical_search_score(tokens, 'photo.jpg', metadata, {}) > 0
