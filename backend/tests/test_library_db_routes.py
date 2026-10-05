"""Gallery list/filter, "on this day" and typeahead served from the library's
local SQLite database (flat memory) instead of a loaded listing blob.

Parity tests pin the SQL ordering/filtering to the in-memory logic it replaced."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest

import app
import ordering_utils
import search_db
from routes.photos import filter_photos, list_photos


def _rows(n=60, seed=7):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        day = rnd.randint(1, 28)
        row = {
            'PartitionKey': 'lib', 'RowKey': f'{rnd.choice("aAbBcC")}img_{i:03d}.jpg',
            'rating': rnd.randint(0, 5), 'likes': rnd.randint(0, 3),
            'uploadDate': f'202{rnd.randint(0, 3)}-0{rnd.randint(1, 9)}-{day:02d}T0{rnd.randint(0, 9)}:00:00+00:00',
            'processing_complete': True,
        }
        if rnd.random() < 0.7:
            row['exifData'] = json.dumps({'DateTimeOriginal': f'201{rnd.randint(5, 9)}:0{rnd.randint(1, 9)}:{day:02d} 10:00:00'})
        if rnd.random() < 0.5:
            row['latitude'], row['longitude'] = str(rnd.uniform(-5, 5)), str(rnd.uniform(-5, 5))
        rows.append(row)
    # an undated photo and duplicates in the sort keys (tie-break coverage)
    rows.append({'PartitionKey': 'lib', 'RowKey': 'zz_undated.jpg', 'rating': 3, 'likes': 1})
    return rows


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    rows = _rows()
    path = str(tmp_path_factory.mktemp('db') / 'lib.sqlite')
    search_db.build_database(rows, path)
    return search_db.SearchDatabase(path), rows


@pytest.mark.parametrize('sort', ['capture', 'date', 'rating', 'likes', 'location', 'something-unknown'])
def test_sql_ordering_matches_order_photo_entries(built, sort):
    db, rows = built
    expected = ordering_utils.order_photo_entries([r['RowKey'] for r in rows], {r['RowKey']: r for r in rows}, sort)
    names, total = db.list_page(sort=sort, limit=1000)
    assert total == len(rows) and names == expected


def test_sql_paging_is_a_window_of_the_same_order(built):
    db, rows = built
    full, _ = db.list_page(sort='capture', limit=1000)
    page, total = db.list_page(sort='capture', offset=10, limit=7)
    assert page == full[10:17] and total == len(rows)


def test_capture_range_matches_the_in_memory_range_check(built):
    db, rows = built
    start, end = datetime(2017, 1, 1, tzinfo=timezone.utc), datetime(2018, 12, 31, tzinfo=timezone.utc)
    expected = [n for n in ordering_utils.order_photo_entries([r['RowKey'] for r in rows], {r['RowKey']: r for r in rows}, 'capture')
                if app._capture_in_range({r['RowKey']: r for r in rows}[n], start, end)]
    names, total = db.list_page(sort='capture', limit=1000,
                                capture_start_day=start.date().toordinal(), capture_end_day=end.date().toordinal())
    assert names == expected and total == len(expected) > 0


def _reference_filter(rows, min_rating, min_likes, lat=None, lon=None, radius_km=0.0):
    """The in-memory filter_photos logic this replaced (photos without coordinates now excluded from location filters)."""
    by_name = {r['RowKey']: r for r in rows}
    ordered = sorted(rows, key=lambda r: r['RowKey'])
    ordered.sort(key=lambda r: ordering_utils.metadata_upload_datetime(r) or ordering_utils.DATE_MIN, reverse=True)
    ordered.sort(key=lambda r: (r.get('rating', 0), r.get('likes', 0)), reverse=True)
    out = []
    for photo in ordered:
        if photo.get('rating', 0) < min_rating or photo.get('likes', 0) < min_likes:
            continue
        if lat is not None and lon is not None:
            try:
                d = ((float(photo['latitude']) - lat) ** 2 + (float(photo['longitude']) - lon) ** 2) ** 0.5
            except (KeyError, ValueError):
                continue  # no coordinates -> not in a location filter
            if d > radius_km * 0.01:
                continue
        out.append(photo['RowKey'])
    return out


@pytest.mark.parametrize('args', [
    dict(min_rating=0, min_likes=0), dict(min_rating=3, min_likes=0), dict(min_rating=2, min_likes=2),
    dict(min_rating=0, min_likes=0, lat=0.0, lon=0.0, radius_km=200.0),
    dict(min_rating=1, min_likes=1, lat=2.0, lon=-1.0, radius_km=150.0),
])
def test_sql_filter_matches_the_in_memory_filter(built, args):
    db, rows = built
    expected = _reference_filter(rows, **args)
    names, total = db.filter_page(
        min_rating=args['min_rating'], min_likes=args['min_likes'], limit=1000,
        latitude=args.get('lat'), longitude=args.get('lon'), radius_degrees=args.get('radius_km', 0.0) * 0.01,
    )
    assert names == expected and total == len(expected)


# --- routes ------------------------------------------------------------------------

def _wire(monkeypatch, db, deleted=()):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_open_library_db', lambda uid: db)
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: {
        'RowKey': name, 'rating': 4, 'size': 10, 'processing_state': 'deleted' if name in deleted else '', 'uploadDate': 'x'})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not load the library listing'))
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user', boom)
    monkeypatch.setattr(app, '_cached_sorted_metadata_list_rows_for_user', boom)
    monkeypatch.setattr(app, 'get_user_listing_index', boom)


def _get(view, url):
    with app.app.test_request_context(url):
        response = view()
    return response.get_json() if hasattr(response, 'get_json') else response[0].get_json()


def test_list_route_pages_from_sql_with_fresh_page_metadata(monkeypatch, built):
    db, rows = built
    _wire(monkeypatch, db)
    payload = _get(list_photos, '/api/photos?sort=rating&offset=5&limit=4')
    assert payload['total'] == len(rows) and len(payload['photos']) == 4
    assert [p['filename'] for p in payload['photos']] == db.list_page(sort='rating', offset=5, limit=4)[0]
    assert all(p['rating'] == 4 for p in payload['photos'])  # fresh from the table, not the (older) database copy


def test_list_route_drops_photos_deleted_since_the_index_was_built(monkeypatch, built):
    db, _ = built
    first = db.list_page(sort='capture', limit=3)[0]
    _wire(monkeypatch, db, deleted={first[0]})
    names = [p['filename'] for p in _get(list_photos, '/api/photos?limit=3')['photos']]
    assert first[0] not in names and len(names) == 2


def test_filter_route_uses_sql_and_reports_total(monkeypatch, built):
    db, rows = built
    _wire(monkeypatch, db)
    payload = _get(filter_photos, '/api/photos/filter?minRating=3&minLikes=1&offset=0&limit=5')
    expected = _reference_filter(rows, 3, 1)
    assert payload['total'] == len(expected)
    assert [p['filename'] for p in payload['photos']] == expected[:5]


def test_list_and_filter_report_index_building_when_no_database_yet(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_open_library_db', lambda uid: None)
    assert _get(list_photos, '/api/photos')['indexBuilding'] is True
    assert _get(filter_photos, '/api/photos/filter')['indexBuilding'] is True


def test_on_this_day_picks_the_best_past_year_from_sql(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    def row(i, year):
        return {'RowKey': f'p{i}.jpg', 'uploadDate': f'{year:04d}-{now.month:02d}-{now.day:02d}T08:00:00+00:00'}
    rows = [row(1, 2019), row(2, 2019), row(3, 2019), row(4, 2021), {'RowKey': 'today.jpg', 'uploadDate': now.isoformat()}]
    path = str(tmp_path / 'otd.sqlite')
    search_db.build_database(rows, path)
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: search_db.SearchDatabase(path))
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no listing')))
    suggestion = app._compute_on_this_day_suggestion('owner')
    assert suggestion['id'] == f'on_this_day:2019-{now.month:02d}-{now.day:02d}' and '3 photos' in suggestion['subtitle']
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    assert app._compute_on_this_day_suggestion('owner') is None


def test_typeahead_completes_places_from_the_database_vocabulary(monkeypatch, tmp_path):
    path = str(tmp_path / 't.sqlite')
    search_db.build_database([{'RowKey': 'a.jpg', 'locationCity': 'Lisbon', 'locationCountry': 'Portugal', 'uploadDate': '2020-01-01T00:00:00+00:00'}], path)
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: search_db.SearchDatabase(path))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({'p1': 'Liam'}, {}))
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no listing')))
    labels = {(s['type'], s['label']) for s in app._search_typeahead_suggestions('owner', 'li')}
    assert ('person', 'Liam') in labels and ('place', 'Lisbon') in labels


def test_point_reads_mode_never_uses_the_or_filter_query(monkeypatch):
    import app as app_module
    from tests.fakes import FakeTable
    table = FakeTable()
    for i in range(40):
        table.upsert_entity({'PartitionKey': 'u', 'RowKey': f'p{i}.jpg', 'rating': i})
    queried = []
    original = table.query_entities
    table.query_entities = lambda *a, **k: (queried.append(a), original(*a, **k))[1]
    monkeypatch.setattr(app_module, 'metadata_table_client', table)
    out = app_module._get_metadata_entities('u', [f'p{i}.jpg' for i in range(40)] + ['missing.jpg'], point_reads=True)
    assert queried == [] and out['p7.jpg']['rating'] == 7 and out['missing.jpg'] is None
