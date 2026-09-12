"""Unit tests for search_photos()'s three explicit tiers (app.py):
_row_passes_search_filters, _score_search_row, _search_row_belongs_in_fallback_bucket.

Extracted from one large inline loop into named tiers -- mirroring Apple's
Core Spotlight filtered/semantic/ranked separation -- specifically so a hard
filter, a scoring signal, and a ranking/bucketing decision can never again
accidentally gate one another the way two real bugs in this function's
history did (a zero lexical score used to veto semantic scoring outright;
a location filler word used to veto an otherwise-perfect match). These
tests pin that invariant directly, not just via the full route.
"""
from __future__ import annotations

import app


def _row(**overrides) -> dict:
    return {
        'RowKey': 'photo.jpg', 'tags': '[]', 'objects': '[]', 'peopleIds': '[]',
        'peopleNames': '[]', 'caption': '', 'ocrText': '', 'address': '',
        'locationCity': '', 'locationRegion': '', 'locationCountry': '',
        **overrides,
    }


# --- _row_passes_search_filters ---------------------------------------------

def test_passes_with_no_filters_active():
    assert app._row_passes_search_filters(_row(), None, None, [], []) is True


def test_fails_capture_range(monkeypatch):
    monkeypatch.setattr(app, '_capture_in_range', lambda row, start, end: False)
    assert app._row_passes_search_filters(_row(), 'start', 'end', [], []) is False


def test_requires_every_queried_person_group_present():
    row = _row(peopleIds='["p1", "p2"]')
    # "alice and bob" -> two groups, each needs at least one matching id
    assert app._row_passes_search_filters(row, None, None, [['p1'], ['p2']], []) is True
    assert app._row_passes_search_filters(row, None, None, [['p1'], ['p3']], []) is False


def test_fails_location_mismatch(monkeypatch):
    monkeypatch.setattr(app, '_metadata_matches_locations', lambda row, terms: False)
    assert app._row_passes_search_filters(_row(), None, None, [], ['paris']) is False


# --- _score_search_row: the core "no signal vetoes another" invariant -------

def test_semantic_score_still_computed_when_lexical_score_is_zero(monkeypatch):
    monkeypatch.setattr(app, 'build_semantic_text', lambda filename, row: 'text')
    monkeypatch.setattr(app, 'lexical_search_score', lambda *a, **k: 0.0)

    score, lexical_score, _ = app._score_search_row(
        {}, 'photo.jpg', _row(), {},
        query_embedding=[1.0, 0.0],
        vector_scores={'photo.jpg': 0.9},
        current_embedding_version='v1',
        semantic_threshold=0.16,
        matched_person_groups=[],
        matched_location_terms=[],
    )

    assert lexical_score == 0.0
    assert score > 0  # rescued entirely by the semantic signal


def test_lexical_score_still_counts_when_semantic_score_is_zero(monkeypatch):
    monkeypatch.setattr(app, 'build_semantic_text', lambda filename, row: 'text')
    monkeypatch.setattr(app, 'lexical_search_score', lambda *a, **k: 5.0)

    score, lexical_score, _ = app._score_search_row(
        {}, 'photo.jpg', _row(), {},
        query_embedding=[],  # no query embedding at all -> semantic_score stays 0.0
        vector_scores={},
        current_embedding_version='v1',
        semantic_threshold=0.16,
        matched_person_groups=[],
        matched_location_terms=[],
    )

    assert lexical_score == 5.0
    assert score == 5.0


def test_person_and_location_bonuses_applied(monkeypatch):
    monkeypatch.setattr(app, 'build_semantic_text', lambda filename, row: 'text')
    monkeypatch.setattr(app, 'lexical_search_score', lambda *a, **k: 0.0)

    score, _, _ = app._score_search_row(
        {}, 'photo.jpg', _row(), {},
        query_embedding=[], vector_scores={}, current_embedding_version='v1',
        semantic_threshold=0.16, matched_person_groups=[['p1'], ['p2']], matched_location_terms=['paris'],
    )

    assert score == 8.0 * 2 + 5.0


# --- _search_row_belongs_in_fallback_bucket ----------------------------------

def test_no_context_intent_never_falls_back():
    assert app._search_row_belongs_in_fallback_bucket(10.0, 10.0, '', {}, 'photo.jpg', _row(), has_context_intent=False) is False


def test_context_intent_falls_back_without_exact_modifier_match():
    tokens = {'modifiers': ['red']}
    row = _row(caption='a blue car')  # modifier "red" never appears anywhere
    assert app._search_row_belongs_in_fallback_bucket(10.0, 10.0, 'blue car', tokens, 'photo.jpg', row, has_context_intent=True) is True


def test_context_intent_stays_primary_with_exact_modifier_match_and_high_lexical_score():
    tokens = {'modifiers': ['red']}
    row = _row(caption='a red car')
    # lexical_score >= 12.0 so only the modifier-match branch is under test,
    # not the separate lexical-score-threshold fallback checked below.
    assert app._search_row_belongs_in_fallback_bucket(20.0, 15.0, 'red car', tokens, 'photo.jpg', row, has_context_intent=True) is False


def test_context_intent_without_modifiers_uses_lexical_score_threshold():
    tokens = {'modifiers': []}
    assert app._search_row_belongs_in_fallback_bucket(20.0, 5.0, '', tokens, 'photo.jpg', _row(), has_context_intent=True) is True
    assert app._search_row_belongs_in_fallback_bucket(20.0, 15.0, '', tokens, 'photo.jpg', _row(), has_context_intent=True) is False
