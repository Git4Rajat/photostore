"""Unit tests for PEOPLE_REP_BOUNDED_EXEMPLARS (Milestone 3's rep-embedding
refresh cap).

_compute_rep_embedding_for_face_ids re-reads and re-averages EVERY face a
person currently owns on every single new-face assignment to them -- the
"popular person" cost. These tests cover the selection mechanics
(_select_bounded_exemplar_ids) and the flag's wiring, which are correctness
properties independent of real embedding data. The actual accuracy
trade-off (does averaging a bounded sample drift the rep embedding enough to
hurt matching) requires validation against a real library's embeddings --
see scripts/compare_bounded_exemplars.py -- which is why the flag defaults
off; these tests don't attempt to substitute for that.
"""
from __future__ import annotations

import json

import pytest

import app


def _face(confidence, confirmed=False):
    return {'confidence': confidence, 'confirmedByUser': confirmed}


def test_select_bounded_exemplar_ids_prioritizes_confirmed_then_confidence():
    summary = {
        'low': _face(0.5),
        'high': _face(0.95),
        'confirmed_low': _face(0.3, confirmed=True),
        'mid': _face(0.7),
    }
    selected = app._select_bounded_exemplar_ids(
        ['low', 'high', 'confirmed_low', 'mid'], summary, cap=2,
    )
    # Confirmed faces always rank ahead of unconfirmed ones regardless of
    # confidence, then ties broken by confidence descending.
    assert selected == ['confirmed_low', 'high']


def test_select_bounded_exemplar_ids_returns_all_when_under_cap():
    summary = {'a': _face(0.5), 'b': _face(0.9)}
    selected = app._select_bounded_exemplar_ids(['a', 'b'], summary, cap=10)
    assert set(selected) == {'a', 'b'}
    assert len(selected) == 2


def test_select_bounded_exemplar_ids_missing_summary_entry_ranks_last():
    summary = {'known': _face(0.9)}
    selected = app._select_bounded_exemplar_ids(['unknown', 'known'], summary, cap=2)
    assert selected == ['known', 'unknown']


@pytest.fixture
def rep_ctx(monkeypatch):
    monkeypatch.setattr(app, '_face_embedding_allowed_for_clustering',
                        lambda face: face.get('alignmentMethod', 'landmark-5pt') == 'landmark-5pt')
    monkeypatch.setattr(app, 'get_face_embeddings_batch', lambda user_id, ids: {
        fid: [1.0, 0.0, 0.0] for fid in ids
    })
    captured = {}

    def _fake_compute(face_entities, np):
        captured['face_entities'] = face_entities
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(app, '_compute_rep_embedding', _fake_compute)
    return captured


def test_bounded_exemplars_disabled_by_default_uses_full_membership(rep_ctx, monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS', False)
    summary = {
        f'f{i}': {'RowKey': f'f{i}', 'personId': 'p1', 'confidence': 0.8, 'embedding': json.dumps([1.0, 0.0, 0.0])}
        for i in range(100)
    }
    app._compute_rep_embedding_for_face_ids('u1', list(summary.keys()), 'p1', face_summary=summary)
    assert len(rep_ctx['face_entities']) == 100


def test_bounded_exemplars_enabled_caps_face_count(rep_ctx, monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS', True)
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS_CAP', 10)
    summary = {
        f'f{i}': {'RowKey': f'f{i}', 'personId': 'p1', 'confidence': 0.8, 'embedding': json.dumps([1.0, 0.0, 0.0])}
        for i in range(100)
    }
    app._compute_rep_embedding_for_face_ids('u1', list(summary.keys()), 'p1', face_summary=summary)
    assert len(rep_ctx['face_entities']) == 10


def test_invalid_faces_do_not_consume_exemplar_cap(rep_ctx, monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS', True)
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS_CAP', 1)
    summary = {
        'rejected': {'RowKey': 'rejected', 'personId': 'p1', 'confidence': 1.0, 'rejected': True},
        'other': {'RowKey': 'other', 'personId': 'p2', 'confidence': 1.0},
        'incompatible': {'RowKey': 'incompatible', 'personId': 'p1', 'confidence': 1.0,
                         'alignmentMethod': 'none'},
        'valid': {'RowKey': 'valid', 'personId': 'p1', 'confidence': 0.8},
    }
    app._compute_rep_embedding_for_face_ids('u1', list(summary), 'p1', summary)
    assert [face['RowKey'] for face in rep_ctx['face_entities']] == ['valid']


def test_exemplar_cap_must_be_positive():
    with pytest.raises(ValueError):
        app._select_bounded_exemplar_ids(['f'], {}, 0)


def test_bounded_exemplars_enabled_noop_when_under_cap(rep_ctx, monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS', True)
    monkeypatch.setattr(app, 'PEOPLE_REP_BOUNDED_EXEMPLARS_CAP', 50)
    summary = {
        f'f{i}': {'RowKey': f'f{i}', 'personId': 'p1', 'confidence': 0.8, 'embedding': json.dumps([1.0, 0.0, 0.0])}
        for i in range(10)
    }
    app._compute_rep_embedding_for_face_ids('u1', list(summary.keys()), 'p1', face_summary=summary)
    assert len(rep_ctx['face_entities']) == 10
