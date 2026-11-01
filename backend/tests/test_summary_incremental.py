"""Differential tests against the untouched full streaming builders."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import random
import sqlite3
import tracemalloc

import pytest

import app
import search_db
import search_utils
import summary_incremental as si
from timeline_metadata import build_timeline_summary


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def row(name, **fields):
    return {'RowKey': name, 'uploadDate': '2020-05-01T12:00:00+00:00', **fields}


def contribution(metadata, position, identity=None):
    return si.contribution_from_row(metadata, identity=identity or f'id-{position}', position=position)


@pytest.fixture
def hydration(monkeypatch):
    fetched = []

    def fetch(name):
        fetched.append(name)
        return {'RowKey': name}

    def photo(name, metadata):
        return {'filename': name, 'fromMetadata': metadata['RowKey']}

    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: fetch(name))
    monkeypatch.setattr(app, '_build_photo_summary', lambda uid, name, metadata, **kwargs: photo(name, metadata))
    return fetch, photo, fetched


def assert_equivalent(store, rows, hydration, now=NOW):
    acc = app.ExploreAccumulator()
    for metadata in rows:
        acc.add(metadata)
    fetch, photo, _ = hydration
    assert store.explore_summary(fetch_metadata=fetch, build_photo=photo) == acc.finalize('user')
    assert store.timeline_summary(now=now) == build_timeline_summary(rows, now=now)


def test_seed_reopen_and_full_builder_semantics(tmp_path, hydration):
    rows = [
        row('first', locationCity='  Malmö ', locationCountry='sweden', latitude='55.01', longitude='13.01',
            tags='["dogs", "DOG", "two words", "automobiles"]', objects=['car', 'cats'], likes='0'),
        row('second', locationCity='malmo', locationCountry='Sweden', latitude='55.99', longitude='13.99',
            tags=['dog', 'cat'], likes='0.9'),
        row('address', address='  main_street ', objects='malformed', likes='invalid'),
        row('coords', latitude='12.345678901', longitude='98.765432109', tags=['photo']),
        row('future', uploadDate='2026-11-02T00:00:00+00:00', likes='10'),
        {'RowKey': 'undated', 'exifData': 'bad'},
        {'RowKey': ''},  # timeline counts this; Explore doesn't
        row('   ', tags=['dog']),  # Explore uses raw RowKey, timeline strips it
        row('local-day', exifData=json.dumps({'DateTimeOriginal': '2020:01:01 00:30:00+05:30'})),
        row('fallback', clientLastModified='2019-04-03T12:00:00+00:00'),
        row('quicktime', exifData=json.dumps({'CreationDate': '2018:02:03 12:00:00-05:00'})),
    ]
    path = str(tmp_path / 'summaries.sqlite')
    with si.SummaryContributionStore(path) as store:
        store.seed((contribution(r, p) for p, r in enumerate(rows)), source_version='v1')
        assert_equivalent(store, rows, hydration)
        assert store.timeline_summary(now=NOW)['undatedCount'] == 2
        assert store.explore_groups()['places'][0]['filename'] == 'first'
        assert store.explore_groups()['places'][0]['latitude'] == '55.01'
        assert {g['label'].lower() for g in store.explore_groups()['things']} >= {'dog', 'photo'}
        assert 'two words' not in {g['label'].lower() for g in store.explore_groups()['things']}
    with si.SummaryContributionStore(path) as store:
        assert_equivalent(store, rows, hydration)
        assert store.get('id-0') == contribution(rows[0], 0)


def test_delete_first_cover_promotes_label_coords_and_disappearing_groups(tmp_path, hydration):
    rows = [row('first', locationCity='malmö', latitude='1', longitude='2', tags=['dog', 'solo']),
            row('second', locationCity='MALMO', latitude='3', longitude='4', tags=['dog']),
            row('third', locationCity='other', tags=['cat'])]
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed((contribution(r, p) for p, r in enumerate(rows)), source_version='v1')
        store.apply([(contribution(rows[0], 0), None)], previous_source_version='v1', source_version='v2')
        assert_equivalent(store, rows[1:], hydration)
        first = store.explore_groups()['places'][0]
        assert first == {'label': 'MALMO', 'filename': 'second', 'count': 1, 'latitude': '3', 'longitude': '4'}
        assert 'Solo' not in [g['label'] for g in store.explore_groups()['things']]
        store.apply([(contribution(rows[1], 1), None)], previous_source_version='v2', source_version='v3')
        assert_equivalent(store, rows[2:], hydration)
        assert store._conn.execute("SELECT count FROM groups WHERE kind=? AND key=?", ('places', 'malmo')).fetchone() is None
        store.apply([(contribution(rows[2], 2), None)], previous_source_version='v3', source_version='v4')
        assert_equivalent(store, [], hydration)
        assert store._conn.execute('SELECT count(*) FROM groups').fetchone()[0] == 0
        assert store._conn.execute('SELECT count(*) FROM days').fetchone()[0] == 0


def test_timeline_cover_deletion_likes_threshold_move_and_rollover(tmp_path, hydration):
    rows = [row('a', likes='0.9'), row('b', likes='1'), row('c', likes='10'),
            row('future', uploadDate='2026-11-02T00:00:00+00:00', likes='100')]
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed((contribution(r, p) for p, r in enumerate(rows)), source_version='v1')
        assert_equivalent(store, rows, hydration)
        summary = store.timeline_summary(now=NOW)
        winner = summary['years']['2020']['coverFilename']
        assert winner in ('b', 'c')  # likes are boolean int(float(x)) > 0, not magnitude
        index = next(p for p, r in enumerate(rows) if r['RowKey'] == winner)
        store.apply([(contribution(rows[index], index), None)], previous_source_version='v1', source_version='v2')
        del_row = rows[index]
        survivors = [r for r in rows if r is not del_row]
        assert_equivalent(store, survivors, hydration)
        replacement = row('a', uploadDate='2025-02-01T00:00:00+00:00', likes='1.1')
        store.apply([(contribution(rows[0], 0), contribution(replacement, 0))],
                    previous_source_version='v2', source_version='v3')
        survivors = [replacement if r['RowKey'] == 'a' else r for r in survivors]
        assert_equivalent(store, survivors, hydration)
        assert_equivalent(store, survivors, hydration, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
        assert store.timeline_summary(now=datetime(2026, 11, 2, tzinfo=timezone.utc))['futureCount'] == 0


def test_stable_count_ties_follow_explicit_order_not_insert_order(tmp_path, hydration):
    rows = [row('early', tags=['zebra', 'dog']), row('late', tags=['cat'])]
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed([contribution(rows[1], 20), contribution(rows[0], 10)], source_version='v1')
        assert_equivalent(store, rows, hydration)
        assert [g['label'] for g in store.explore_groups()['things']] == ['Zebra', 'Dog', 'Cat']
        updated = row('early', tags=['new', 'dog'])
        store.apply([(contribution(rows[0], 10), contribution(updated, 10))],
                    previous_source_version='v1', source_version='v2')
        assert_equivalent(store, [updated, rows[1]], hydration)
        assert [g['label'] for g in store.explore_groups()['things']] == ['New', 'Dog', 'Cat']


def test_shared_parser_caps_and_no_added_confidence_threshold(tmp_path, monkeypatch, hydration):
    monkeypatch.setattr(search_utils, 'MAX_TAGS_STORED', 2)
    monkeypatch.setattr(search_utils, 'MAX_TAG_LENGTH', 5)
    metadata = row('caps', tags=['abcdefg', 'sky', 'dog'], objects=['abcdefg', 'cat', 'bird'],
                   processing_metadata=json.dumps({'client_ai_vision': {
                       'predictions': [{'label': 'sky', 'score': 0.0}]}}))
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed([contribution(metadata, 0)], source_version='v1')
        assert_equivalent(store, [metadata], hydration)
        assert store.explore_groups()['things'] == [
            {'label': 'Abcde', 'filename': 'caps', 'count': 1},
            {'label': 'Sky', 'filename': 'caps', 'count': 1},
            {'label': 'Cat', 'filename': 'caps', 'count': 1},
        ]


def test_multiple_occurrences_per_group_count_and_earliest_candidate(tmp_path, monkeypatch, hydration):
    # Preserve the accumulator's dedup-before-place-normalization behavior,
    # even if shared parsers someday return distinct spellings of one term.
    monkeypatch.setattr(app, 'parse_tags', lambda raw: ['résumé', 'resume', 'résumé'])
    monkeypatch.setattr(si, 'parse_tags', app.parse_tags)
    metadata = row('weighted')
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed([contribution(metadata, 0)], source_version='v1')
        assert_equivalent(store, [metadata], hydration)
        assert store.explore_groups()['things'] == [{'label': 'Résumé', 'filename': 'weighted', 'count': 2}]
        store.apply([(contribution(metadata, 0), None)], previous_source_version='v1', source_version='v2')
        assert store.explore_groups()['things'] == []


def test_missing_metadata_does_not_refill_top_24_or_fetch_duplicates(tmp_path, monkeypatch, hydration):
    rows = [row(f'p{p}', locationCity=f'city{p}', tags=[f'tag{p}']) for p in range(30)]
    fetch, photo, _ = hydration
    fetched = []

    def missing(name):
        fetched.append(name)
        return None if name == 'p0' else fetch(name)

    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: missing(name))
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed((contribution(r, p) for p, r in enumerate(rows)), source_version='v1')
        acc = app.ExploreAccumulator()
        for metadata in rows:
            acc.add(metadata)
        expected = acc.finalize('u')
        fetched.clear()
        assert store.explore_summary(fetch_metadata=missing, build_photo=photo) == expected
        assert len(fetched) == 24 and len(set(fetched)) == 24
        assert len(expected['places']) == len(expected['things']) == 23


def test_random_differential_updates(tmp_path, hydration):
    rng = random.Random(729)

    def make(p):
        return row(f'photo{p}', locationCity=rng.choice(['malmö', 'MALMO', 'paris', '', 'New_York']),
                   address=rng.choice(['', 'main street']), latitude=str(p), longitude=str(p + 1),
                   tags=rng.sample(['dog', 'dogs', 'cat', 'two words', 'photo', 'cars'], 3),
                   objects=rng.sample(['dog', 'cat', 'tree'], 2), likes=rng.choice(['1', '0.9', '-1', 'oops']),
                   uploadDate=rng.choice(['2019-01-01T00:00:00Z', '2020-02-29T00:00:00Z', '',
                                          '2026-11-02T00:00:00Z']))

    live = {p: make(p) for p in range(60)}
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed((contribution(r, p) for p, r in live.items()), source_version='v0')
        for version in range(1, 61):
            p = rng.randrange(80)
            old = contribution(live[p], p) if p in live else None
            if p in live and rng.random() < .35:
                del live[p]
                new = None
            else:
                live[p] = make(p)
                new = contribution(live[p], p)
            store.apply([(old, new)], previous_source_version=f'v{version - 1}', source_version=f'v{version}')
            assert_equivalent(store, [live[p] for p in sorted(live)], hydration)


def test_explicit_seed_stale_before_and_atomic_rollback(tmp_path, hydration):
    path = str(tmp_path / 's')
    original, inserted = contribution(row('original'), 0), contribution(row("x'; DROP TABLE photos;--"), 1)
    with si.SummaryContributionStore(path) as store:
        with pytest.raises(si.ContributionConflict, match='seed'):
            store.timeline_summary(now=NOW)
        with pytest.raises(si.ContributionConflict):
            store.apply([(None, original)], previous_source_version='missing', source_version='v1')

        def failed_stream():
            yield original
            raise RuntimeError('interrupted full stream')

        with pytest.raises(RuntimeError):
            store.seed(failed_stream(), source_version='v1')
        assert store.get(original.identity) is None and store._version() is None
        with pytest.raises(si.ContributionConflict):
            store.seed([original, original], source_version='v1')
        store.seed([original], source_version='v1')
        with pytest.raises(si.ContributionConflict):
            store.seed([], source_version='reset')
        with pytest.raises(si.ContributionConflict, match='before'):
            store.apply([(None, inserted), (replace(original, filename='stale'), None)],
                        previous_source_version='v1', source_version='v2')
        assert store.get(inserted.identity) is None and store._version() == 'v1'
        assert_equivalent(store, [row('original')], hydration)
        with pytest.raises(sqlite3.IntegrityError):
            store.apply([(None, replace(inserted, position=0))], previous_source_version='v1', source_version='v2')
        assert_equivalent(store, [row('original')], hydration)
        store.apply([(None, inserted)], previous_source_version='v1', source_version='v2')
        assert store.get(inserted.identity) == inserted
        with si.SummaryContributionStore(path) as other:
            with pytest.raises(si.ContributionConflict, match='source'):
                other.apply([(original, None)], previous_source_version='v1', source_version='v3')
        with pytest.raises(ValueError, match='advance'):
            store.apply([], previous_source_version='v2', source_version='v2')


def test_parameterized_indexed_plans_and_no_photo_scan(tmp_path):
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        old = contribution(row('a', tags=['dog'], locationCity="O'Brien"), 0)
        new = contribution(row('a', tags=['cat'], locationCity='Paris'), 0)
        store.seed([old], source_version='v1')
        statements = []
        store._conn.set_trace_callback(statements.append)
        store.apply([(old, new)], previous_source_version='v1', source_version='v2')
        store.explore_groups()
        store.timeline_summary(now=NOW)
        store._conn.set_trace_callback(None)
        reads = [s for s in statements if s.lstrip().upper().startswith('SELECT')]
        assert not any('FROM photos' in s and 'WHERE identity=' not in s for s in reads)
        assert not any('FROM members' in s and 'WHERE kind=' not in s for s in reads)
        queries = [
            ('SELECT position,occurrence,payload FROM members WHERE kind=? AND key=? '
             'ORDER BY position,occurrence LIMIT 1', ('things', 'cat'), 'member_candidates'),
            ('SELECT count,payload FROM groups WHERE kind=? '
             'ORDER BY count DESC,first_position,first_occurrence LIMIT ?', ('things', 24), 'top_groups'),
            ('SELECT liked_order,rank,filename FROM covers WHERE day=? '
             'ORDER BY liked_order,rank,filename LIMIT 1', (new.capture_day,), 'day_candidates'),
        ]
        for sql, params, index in queries:
            plan = ' '.join(r[3] for r in store._conn.execute('EXPLAIN QUERY PLAN ' + sql, params))
            assert 'SEARCH' in plan and index in plan and 'TEMP B-TREE' not in plan


def test_seed_memory_is_not_proportional_to_photos(tmp_path):
    peaks = []
    for n in (400, 4000):
        with si.SummaryContributionStore(str(tmp_path / f's{n}')) as store:
            tracemalloc.start()
            try:
                store.seed((contribution(row(f'p{p}', tags=['dog'], unused='x' * 12000), p)
                            for p in range(n)), source_version='v1')
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
            assert store.timeline_summary(now=NOW)['totalCount'] == n
    assert max(peaks) < 2 * 1024 * 1024
    assert peaks[1] < peaks[0] + 512 * 1024


def test_sqlite_timeline_is_explicitly_not_the_streaming_reference(tmp_path):
    metadata = row('timezone', exifData=json.dumps({'DateTimeOriginal': '2020:01:01 00:30:00+05:30'}))
    path = str(tmp_path / 'search.sqlite')
    builder = search_db.DatabaseBuilder(path)
    builder.add(metadata)
    builder.finish()
    db = search_db.SearchDatabase(path)
    try:
        sql_summary = db.timeline_summary(today=NOW.date())
        with si.SummaryContributionStore(str(tmp_path / 'sidecar')) as store:
            store.seed([contribution(metadata, 0)], source_version='v1')
            assert store.timeline_summary(now=NOW) == build_timeline_summary([metadata], now=NOW)
            assert store.timeline_summary(now=NOW)['firstDate'] == '2020-01-01'
            assert sql_summary['firstDate'] == '2019-12-31'
            assert store.timeline_summary(now=NOW) != sql_summary
    finally:
        db._conn().close()


def test_likes_overflow_is_deferred_exactly_until_eligible(tmp_path, hydration):
    rows = [{'RowKey': 'undated', 'likes': 'inf'}, row('', likes='inf'),
            row('future', uploadDate='2026-11-02T00:00:00Z', likes='inf')]
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        store.seed((contribution(r, p) for p, r in enumerate(rows)), source_version='v1')
        assert_equivalent(store, rows, hydration)
        later = datetime(2026, 11, 2, tzinfo=timezone.utc)
        with pytest.raises(OverflowError):
            build_timeline_summary(rows, now=later)
        with pytest.raises(OverflowError):
            store.timeline_summary(now=later)
        # An exceptional read must not poison the connection's transaction.
        assert_equivalent(store, rows, hydration)


@pytest.mark.parametrize('position', [-1, True, 2**63])
def test_invalid_position_fails_without_partial_seed(tmp_path, position):
    with si.SummaryContributionStore(str(tmp_path / 's')) as store:
        with pytest.raises(ValueError, match='position'):
            store.seed([replace(contribution(row('a'), 0), position=position)], source_version='v1')
        assert store.get('id-0') is None and store._version() is None