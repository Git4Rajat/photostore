"""Exact differentials against pre-integration full Explore and SQL timeline."""
from datetime import date, datetime, timezone
import json
import random
import sqlite3
import threading

import pytest

import app
import search_db
import summary_incremental as si
import summary_runtime as runtime
from tests.test_search_db_delta import world  # real base/delta/compaction blob fixture
from tests.test_summary_incremental import hydration
from timeline_metadata import TimelineAccumulator


TODAY = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(search_db, 'datetime', FixedDateTime)


def row(name, **fields):
    return {'RowKey': name, 'uploadDate': '2020-05-01T12:00:00+00:00', **fields}


def database(tmp_path, rows):
    path = str(tmp_path / 'search.sqlite')
    search_db.build_database(iter(rows), path)
    return search_db.SearchDatabase(path)


def sql_timeline_reference(db, today=TODAY):
    """The previous production SQL algorithm, not TimelineAccumulator.

    capture_day is UTC but capture_year/md come from original capture time;
    covers must keep that intentional mismatch, likes boolean and SHA rank.
    """
    conn = db._conn()
    total = int(conn.execute('SELECT COUNT(*) FROM rows').fetchone()[0])
    undated = int(conn.execute('SELECT COUNT(*) FROM rows WHERE capture_day IS NULL').fetchone()[0])
    years, future, first, last = {}, 0, None, None
    for ordinal, count in conn.execute('SELECT capture_day, COUNT(*) FROM rows WHERE capture_day IS NOT NULL GROUP BY capture_day'):
        day = date.fromordinal(int(ordinal))
        if day > today:
            future += int(count)
            continue
        year_bucket = years.setdefault(f'{day.year:04d}', {'count': 0, 'months': {}})
        month_bucket = year_bucket['months'].setdefault(f'{day.month:02d}', {'count': 0, 'days': {}})
        year_bucket['count'] += int(count)
        month_bucket['count'] += int(count)
        month_bucket['days'][f'{day.day:02d}'] = int(count)
        first = day if first is None or day < first else first
        last = day if last is None or day > last else last
    cumulative, running = {}, 0
    for year in sorted(years):
        running += years[year]['count']
        cumulative[year] = running
    for year_key, y in years.items():
        year = int(year_key)
        cover = conn.execute('SELECT filename FROM rows WHERE capture_year = ? AND capture_day <= ? '
                             'ORDER BY has_likes DESC, cover_rank LIMIT 1', (year, today.toordinal())).fetchone()
        if cover:
            y['coverFilename'] = str(cover[0])
        for month_key, m in y['months'].items():
            month = int(month_key)
            cover = conn.execute('SELECT filename FROM rows WHERE capture_year = ? AND capture_md BETWEEN ? AND ? '
                                 'AND capture_day <= ? ORDER BY has_likes DESC, cover_rank LIMIT 1',
                                 (year, month * 100 + 1, month * 100 + 31, today.toordinal())).fetchone()
            if cover:
                m['coverFilename'] = str(cover[0])
    return {'years': years, 'cumulativeByYear': cumulative, 'firstDate': first.isoformat() if first else None,
            'lastDate': last.isoformat() if last else None, 'today': today.isoformat(),
            'undatedCount': undated, 'futureCount': future, 'totalCount': total}


def full_explore(db):
    acc = app.ExploreAccumulator()
    for metadata in db.iter_smart_rows():
        acc.add(metadata)
    return acc.finalize('user')


def equivalent(db):
    expected_explore = full_explore(db)
    explore, timeline = runtime.finalized_payloads(db, 'user', accumulator_factory=app.ExploreAccumulator)
    assert {key: explore[key] for key in ('places', 'things')} == expected_explore
    assert {key: timeline[key] for key in sql_timeline_reference(db)} == sql_timeline_reference(db)
    assert explore['sourceVersion'] == timeline['sourceVersion']
    assert explore['summaryMode'] == timeline['summaryMode'] == si.DATABASE_FORMAT
    assert db.timeline_summary(today=TODAY) == sql_timeline_reference(db)


def test_exact_reduced_shape_not_raw_stream_and_sql_dates(tmp_path, hydration):
    rows = [
        row(' padded ', tags=['dogs', 'two words'], subjectTags=['cat'], objects=['raw-only'],
            locationCity='Malmö', latitude='55.123456', longitude='13.987654'),
        row('b', tags=['cat'], locationCity='MALMO', latitude='56.5', longitude='14.5', likes='0.9'),
        row('c', tags=['cat'], address='main_street', likes='12'),
        row('utc-boundary', exifData=json.dumps({'DateTimeOriginal': '2020:01:01 00:30:00+05:30'})),
        row('month-boundary', exifData=json.dumps({'DateTimeOriginal': '2020:03:01 00:30:00+05:30'})),
        row('future', uploadDate='2026-11-02T00:00:00+00:00', likes='2'),
        {'RowKey': 'undated', 'exifData': 'bad'},
        {'RowKey': ''},
        {'RowKey': 'reserved', 'pendingAnonymousBlob': 'reserved', 'upload_started_at': '2026-01-01T00:00:00Z'},
        row('quicktime', exifData=json.dumps({'CreationDate': '2018:02:03 12:00:00-05:00'})),
    ]
    db = database(tmp_path, rows)
    equivalent(db)
    # 00:30 on 1 Jan in India is still 1 Jan: counted on the capture's own day (it used to land on
    # 31 Dec 2019 by UTC, in a year with no cover since covers already used the local year).
    assert db.timeline_summary(today=TODAY)['years']['2020']['months']['01']['days']['01'] == 1
    assert '2019' not in db.timeline_summary(today=TODAY)['years']
    assert db.explore_groups()['places'][0]['latitude'] == '55.123456'
    assert all(g['label'] != 'Raw-Only' for g in db.explore_groups()['things'])
    assert db.timeline_summary(today=date(2026, 11, 2)) == sql_timeline_reference(db, date(2026, 11, 2))


def test_random_differential_replacements_deletes_counts_and_cover_ties(tmp_path, hydration):
    rng = random.Random(946321)

    def make(i):
        return row(f'photo{i}', tags=rng.sample(['dog', 'cats', 'photo', 'cars', 'sky', 'résumé'], 3),
                   subjectTags=rng.sample(['dog', 'birds', 'tree'], 2), objects=['not-indexed'],
                   locationCity=rng.choice(['malmö', 'MALMO', 'New_York', '', 'Paris']),
                   latitude=rng.choice(['', '1.25', 'nan']), longitude=rng.choice(['', '2.5']),
                   likes=rng.choice(['0', '0.9', '1.1', '10', '-1', 'bad']),
                   uploadDate=rng.choice(['', '2019-12-31T23:30:00-05:00', '2021-02-01T00:00:00Z',
                                          '2026-11-02T00:00:00Z', 'bad']),
                   exifData=rng.choice(['{}', 'bad', '{"DateTimeOriginal":"2020:01:01 00:01:00+05:30"}']))

    db = database(tmp_path, [make(i) for i in range(50)])
    equivalent(db)
    for seq in range(1, 81):
        names = rng.sample(range(70), 5)
        records = [search_db.row_record(make(i)) for i in names[:3]]
        # Force rank collisions: use the SAME legacy ORDER BY without inventing
        # a filename tiebreaker or replacing SQL period covers with daily covers.
        if seq % 4 == 0:
            for record in records:
                record['cover_rank'] = 0
        db.apply_delta({'seq': seq, 'upserts': records, 'deletes': [f'photo{i}' for i in names[3:]]})
        equivalent(db)
    clone = search_db.SearchDatabase(db.path)
    equivalent(clone)
    assert clone.summary_snapshot()['sourceVersion'] == db.summary_snapshot()['sourceVersion']


def test_missing_winner_no_backfill_and_multiple_normalized_occurrences(tmp_path, monkeypatch, hydration):
    monkeypatch.setattr(app, 'parse_tags', lambda raw: ['résumé', 'resume'])
    monkeypatch.setattr(si, 'parse_tags', app.parse_tags)
    db = database(tmp_path, [row(f'p{i}', locationCity=f'city{i}') for i in range(30)])
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: None if name == 'p0' else {'RowKey': name})
    equivalent(db)
    assert db.explore_groups()['things'][0]['count'] == 60
    explore, _ = runtime.finalized_payloads(db, 'user', accumulator_factory=app.ExploreAccumulator)
    assert len(explore['places']) == 23 and explore['things'] == []
    db.apply_delta({'seq': 1, 'deletes': ['p0']})
    equivalent(db)
    assert db.explore_groups()['things'][0]['filename'] == 'p1'


def test_rollback_rows_aggregates_fts_and_sequence_together(tmp_path, hydration, monkeypatch):
    db = database(tmp_path, [row('a', tags=['dog']), row('b', tags=['cat'])])
    before = db.summary_snapshot()
    original = si.DatabaseSummaryAdapter.change

    def fail_after_change(adapter, *args, **kwargs):
        original(adapter, *args, **kwargs)
        if not kwargs.get('remove'):
            raise RuntimeError('injected aggregate write failure')

    monkeypatch.setattr(si.DatabaseSummaryAdapter, 'change', fail_after_change)
    with pytest.raises(RuntimeError, match='injected'):
        db.apply_delta({'seq': 1, 'upserts': [search_db.row_record(row('a', tags=['bird']))], 'deletes': ['b']})
    assert db.summary_snapshot() == before and db.applied_seq() == 0
    assert db.row_count() == 2
    assert [name for name, _ in db.candidates(['dog'])] == ['a']
    equivalent(db)


def test_snapshot_readers_see_atomic_old_or_new_commits(tmp_path, hydration, monkeypatch):
    db = database(tmp_path, [row('a', tags=['dog'])])
    # WAL permits a writer to finish while a read snapshot is pinned.
    with sqlite3.connect(db.path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    before = db.summary_snapshot()
    ready, committed = threading.Event(), threading.Event()
    errors = []
    original = search_db.SearchDatabase.explore_groups

    def interleave(self, **kwargs):
        groups = original(self, **kwargs)
        ready.set()
        assert committed.wait(10)
        return groups

    def writer():
        try:
            assert ready.wait(10)
            search_db.SearchDatabase(db.path).apply_delta(
                {'seq': 1, 'upserts': [search_db.row_record(row('b', tags=['cat']))]})
        except Exception as exc:
            errors.append(exc)
        finally:
            committed.set()

    monkeypatch.setattr(search_db.SearchDatabase, 'explore_groups', interleave)
    thread = threading.Thread(target=writer)
    thread.start()
    during = db.summary_snapshot()
    thread.join(10)
    assert not thread.is_alive() and errors == []
    assert during == before
    after = db.summary_snapshot()
    assert after['sequence'] == 1 and after['timeline']['totalCount'] == 2
    assert after['sourceVersion'] != before['sourceVersion']


def test_replayed_concurrent_delta_is_noop(tmp_path, hydration):
    db = database(tmp_path, [row('a', tags=['dog'])])
    # Production sync_deltas converts the downloaded base under its existing
    # cross-process download lock. This test races delta TRANSACTIONS on WAL,
    # not SQLite's separately serialized initial journal-mode conversion.
    with sqlite3.connect(db.path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    delta = {'seq': 1, 'upserts': [search_db.row_record(row('b', tags=['cat']))]}
    errors = []

    def apply():
        try:
            search_db.SearchDatabase(db.path).apply_delta(delta)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == [] and db.row_count() == 2 and db.applied_seq() == 1
    equivalent(db)


def test_delta_and_hot_summary_trace_have_no_full_rows_scan(tmp_path, hydration, monkeypatch):
    db = database(tmp_path, [row(f'p{i}', tags=['dog'], locationCity='Paris') for i in range(2000)])
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(search_db.sqlite3, 'connect', traced)
    monkeypatch.setattr(db, 'iter_smart_rows', lambda: pytest.fail('hot-path scan'))
    db.apply_delta({'seq': 1, 'upserts': [search_db.row_record(row('p0', tags=['cat']))], 'deletes': ['p1']})
    runtime.finalized_payloads(db, 'user', accumulator_factory=app.ExploreAccumulator)
    reads = [sql.lower() for sql in statements if sql.lower().startswith('select')]
    assert not any('group by' in sql or 'count(*) from rows' in sql for sql in reads)
    assert all((' where ' in sql or 'max(id)' in sql) for sql in reads if ' from rows' in sql)
    assert any('summary_candidates' in str(r) for r in db._conn().execute(
        "EXPLAIN QUERY PLAN SELECT id,occurrence,payload FROM summary_members WHERE kind='things' AND key='dog' ORDER BY id,occurrence LIMIT 1"))
    assert any('summary_top' in str(r) for r in db._conn().execute(
        "EXPLAIN QUERY PLAN SELECT count,payload FROM summary_groups WHERE kind='things' ORDER BY count DESC,first_id,first_occurrence LIMIT 24"))


def drop_summaries(path):
    with sqlite3.connect(path) as conn:
        for table in ('summary_days', 'summary_members', 'summary_groups'):
            conn.execute(f'DROP TABLE {table}')
        conn.execute("DELETE FROM meta WHERE key IN ('summary_schema','summary_base_id')")


def test_legacy_schema_never_scans_on_read_or_delta(tmp_path, hydration):
    db = database(tmp_path, [row('a')])
    drop_summaries(db.path)
    with pytest.raises(si.SummarySchemaMissing):
        runtime.finalized_payloads(db, 'user', accumulator_factory=app.ExploreAccumulator, allow_full_adapter=True)
    db.apply_delta({'seq': 1, 'upserts': [search_db.row_record(row('b'))]})
    assert db.row_count() == 2
    with pytest.raises(si.SummarySchemaMissing):
        db.timeline_summary(today=TODAY)


def test_explicit_compaction_seeds_legacy_and_replica_backup_keeps_identity(world, hydration):
    db = search_db.open_database('lib')
    drop_summaries(db.path)
    compacted = search_db.compact_database('lib')
    assert compacted
    upgraded = search_db.open_database('lib')
    equivalent(upgraded)
    token = upgraded.summary_snapshot()['sourceVersion']
    assert search_db.compact_database('lib')
    assert search_db.open_database('lib').summary_snapshot()['sourceVersion'] == token


def test_compaction_labels_actual_backup_sequence_when_delta_commits(world, hydration, monkeypatch):
    db = search_db.open_database('lib')
    connect = sqlite3.connect
    raced = []

    class BackupConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            if not raced:
                raced.append(True)
                record = search_db.row_record(row('concurrent', tags=['cat']))
                seq = search_db.publish_delta('lib', [record], [])
                assert seq == 1
                db.apply_delta({'seq': seq, 'upserts': [record]})
            return super().backup(target, **kwargs)

    def wrapped(*args, **kwargs):
        if str(args[0]).startswith('file:'):
            kwargs['factory'] = BackupConnection
        return connect(*args, **kwargs)

    monkeypatch.setattr(search_db.sqlite3, 'connect', wrapped)
    compacted = search_db.compact_database('lib')
    assert compacted['baseSeq'] == compacted['deltaSeq'] == 1
    upgraded = search_db.open_database('lib')
    assert upgraded.applied_seq() == 1 and upgraded.row_count() == 4
    equivalent(upgraded)


def test_app_full_fake_adapter_only_and_incremental_publication_retry(tmp_path, hydration, monkeypatch):
    class Fake:
        def iter_smart_rows(self):
            yield row('fake', tags=['cat'])

        def timeline_summary(self):
            return {'totalCount': 1}

    outputs = []
    monkeypatch.setattr(app, 'store_explore_summary', lambda uid, value: outputs.append(('explore', value)))
    monkeypatch.setattr(app, 'store_timeline_summary', lambda uid, value: outputs.append(('timeline', value)))
    monkeypatch.setattr(app, 'load_explore_summary', lambda uid: next(v for k, v in reversed(outputs) if k == 'explore'))
    monkeypatch.setattr(app, 'load_timeline_summary', lambda uid: next(v for k, v in reversed(outputs) if k == 'timeline'))
    app._finalize_library_summaries('user', Fake())
    assert len(outputs) == 2
    monkeypatch.setattr(search_db, 'open_database', lambda uid: Fake())
    with pytest.raises(RuntimeError, match='indexed'):
        app._refresh_library_summaries('user')
    db = database(tmp_path, [row('a', tags=['dog'])])
    db.apply_delta({'seq': 1, 'upserts': [search_db.row_record(row('b', tags=['cat']))]})
    monkeypatch.setattr(search_db, 'open_database', lambda uid: db)
    app._refresh_library_summaries('user')
    assert outputs[-1][1]['sourceVersion'] == outputs[-2][1]['sourceVersion']
    assert ':1:' in outputs[-1][1]['sourceVersion']
    token = outputs[-1][1]['sourceVersion']

    def fail(uid, payload):
        raise RuntimeError('publication failed')

    # Force repair of a missing output; an unchanged, already-confirmed
    # summary now intentionally avoids the store entirely.
    monkeypatch.setattr(app, 'load_timeline_summary', lambda uid: None)
    monkeypatch.setattr(app, 'store_timeline_summary', fail)
    with pytest.raises(RuntimeError, match='publication'):
        app._refresh_library_summaries('user')
    assert db.summary_snapshot()['sourceVersion'] == token


def test_best_effort_upload_failure_is_not_acknowledged(tmp_path, hydration, monkeypatch):
    db = database(tmp_path, [row('a')])
    monkeypatch.setattr(search_db, 'open_database', lambda uid: db)
    monkeypatch.setattr(app, 'store_explore_summary', lambda uid, value: None)
    monkeypatch.setattr(app, 'load_explore_summary', lambda uid: None)
    with pytest.raises(RuntimeError, match='not confirmed'):
        app._refresh_library_summaries('user')


def test_raw_full_sinks_keep_their_original_algorithms(hydration):
    rows = [row('raw', tags=['dog'], objects=['unfiltered'], latitude='1.23', longitude='4.56'),
            row('boundary', exifData='{"DateTimeOriginal":"2020:01:01 00:01:00+05:30"}')]
    explore, timeline = [], []
    eacc, tacc = app.ExploreAccumulator(), TimelineAccumulator()
    esink = runtime.RawExploreSink('u', 'v1', app.ExploreAccumulator(), lambda u, value: explore.append(value))
    tsink = runtime.RawTimelineSink('u', 'v1', TimelineAccumulator(), lambda u, value: timeline.append(value))
    for metadata in rows:
        esink.add(metadata)
        tsink.add(metadata)
        eacc.add(metadata)
        tacc.add(metadata)
    esink.finish()
    tsink.finish()
    assert {key: explore[0][key] for key in ('places', 'things')} == eacc.finalize('u')
    assert {key: timeline[0][key] for key in tacc.summary()} == tacc.summary()
    assert explore[0]['summaryMode'] == timeline[0]['summaryMode'] == runtime.RAW_FORMAT


def test_today_rollover_advances_snapshot_token_without_delta(tmp_path, hydration):
    db = database(tmp_path, [row('future', uploadDate='2026-11-02T00:00:00Z', likes='1')])
    before = db.summary_snapshot(today=TODAY)
    after = db.summary_snapshot(today=date(2026, 11, 2))
    assert before['sequence'] == after['sequence'] == 0
    assert before['sourceVersion'] != after['sourceVersion']
    assert before['timeline']['futureCount'] == 1 and after['timeline']['futureCount'] == 0
    assert after['timeline'] == sql_timeline_reference(db, date(2026, 11, 2))