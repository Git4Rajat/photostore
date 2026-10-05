"""SQLite (FTS5) search database: reduced rows, build/query, blob + ephemeral-disk
lifecycle, and the /photos/search route that queries it."""
from __future__ import annotations

import json
from datetime import date

import pytest

import app
import search_db
import search_utils
import storage_utils
from routes.photos import search_photos
from tests.fakes import FakeTable
from tests.test_lexical_index import _FakeBlobServiceClient


def _big_row():
    return {
        'PartitionKey': 'lib', 'RowKey': 'a.jpg',
        'tags': json.dumps(['dog', 'grass', 'blurry']), 'subjectTags': json.dumps(['dog']),
        'objects': json.dumps(['dog', 'ball']), 'backgroundTags': json.dumps(['grass']),
        'tagMetadata': json.dumps([
            {'tag': 'dog', 'source': 'ai_tag', 'confidence': 0.9},
            {'tag': 'grass', 'source': 'ai_tag', 'confidence': 0.6},
            {'tag': 'blurry', 'source': 'ai_tag', 'confidence': 0.26},
        ]),
        'caption': 'a dog', 'locationCity': 'Paris', 'faceCount': 1,
        'latitude': '48.856614', 'longitude': '2.352222',
        'ocrText': ('word  ' * 5000), 'weakTags': '["w"]', 'faces': 'f' * 5000,
        'upload_started_at': '2020-01-02T00:00:00+00:00',
        'exifData': json.dumps({'Model': 'Cam', 'DateTimeOriginal': '2020:01:01 10:00:00',
                                'MakerNote': 'z' * 5000, 'GPS.GPSLatitude': '1'}),
        'processing_metadata': json.dumps({
            'client_ai_vision': {'predictions': [{'label': 'cat', 'score': 0.9}, {'label': 'low', 'score': 0.05}]},
            'client_face': {'blob': 'y' * 5000},
        }),
    }


# --- reduced rows ---------------------------------------------------------------

def test_reduced_row_is_scorer_compatible_and_much_smaller():
    full = _big_row()
    slim = search_db.reduced_row(full)
    assert json.loads(slim['subjectTags']) == ['dog']
    assert json.loads(slim['tags']) == ['grass', 'ball']  # 'blurry' (0.26) filtered, objects/background merged
    assert search_utils.prediction_tags(slim) == ['cat']
    assert slim['latitude'] == slim['longitude'] == '1' and slim['uploadDate'] == '2020-01-02T00:00:00+00:00'
    for dropped in ('weakTags', 'faces', 'tagMetadata', 'objects', 'backgroundTags', 'PartitionKey'):
        assert dropped not in slim
    assert len(json.dumps(slim)) < len(json.dumps(full)) / 6
    # the scorer still finds this photo by its (kept) tags and its location
    tokens = search_utils.parse_search_query('dog paris')
    assert search_utils.lexical_search_score(tokens, 'a.jpg', slim, {'Model': 'Cam'}) > 0


# --- build + query --------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    rows = [
        {'RowKey': 'IMG_1.jpg', 'tags': json.dumps(['dog', 'grass']), 'subjectTags': json.dumps(['dog']),
         'locationCity': 'Paris', 'locationCountry': 'France', 'ocrText': 'welcome to the cafe',
         'uploadDate': '2020-05-01T00:00:00+00:00', 'peopleIds': json.dumps(['p1'])},
        {'RowKey': 'IMG_2.jpg', 'tags': json.dumps(['beach', 'sunset']), 'subjectTags': json.dumps(['beach']),
         'locationCity': 'Goa', 'uploadDate': '2021-01-01T00:00:00+00:00'},
        {'RowKey': 'IMG_3.jpg', 'tags': json.dumps(['dogs']), 'uploadDate': '2022-01-01T00:00:00+00:00',
         'peopleIds': json.dumps(['p2'])},
        {'RowKey': '', 'tags': '[]'},  # no filename -> skipped
    ]
    path = str(tmp_path / 's.sqlite')
    assert search_db.build_database(rows, path) == 3
    return search_db.SearchDatabase(path)


def _names(db, query, **kw):
    return [f for f, _ in db.candidates(search_db.match_terms(search_utils.parse_search_query(query)), **kw)]


def test_candidates_match_tags_plurals_ocr_and_prefixes(db):
    assert set(_names(db, 'dog')) == {'IMG_1.jpg', 'IMG_3.jpg'}      # 'dogs' via plural variant
    assert _names(db, 'cafe') == ['IMG_1.jpg']                        # OCR text is searchable
    assert _names(db, 'xyzzy') == []
    assert _names(db, 'caf') == []                                    # short tokens are exact...
    assert _names(db, 'welcom') == ['IMG_1.jpg']                      # ...4+ chars match as prefixes


def test_person_rows_and_capture_range_narrow_candidates(db):
    assert [f for f, _ in db.candidates([], person_ids=['p2'])] == ['IMG_3.jpg']
    start = date(2021, 6, 1).toordinal()
    assert _names(db, 'dog', capture_start_day=start) == ['IMG_3.jpg']


def test_location_vocabulary_is_stored(db):
    assert {'paris', 'france', 'goa'} <= set(db.location_terms())


def test_hostile_query_text_cannot_break_the_fts_expression():
    terms = search_db.match_terms({'all': ['a"b', 'x* OR y', 'ok"; DROP TABLE rows;--']})
    assert terms and all(t.isalnum() for t in terms)  # quotes/operators never reach the MATCH expression
    assert '"' not in ''.join(terms) and '*' not in ''.join(terms)
    assert search_db.fts_expression(['dog', 'beach']) == '"dog" OR "beach"*'


# --- blob + ephemeral-disk lifecycle ---------------------------------------------

@pytest.fixture
def blobs(monkeypatch, tmp_path):
    svc = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', svc)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setattr(search_db, 'SEARCH_DB_DIR', str(tmp_path / 'ephemeral'))
    search_db._OPEN.clear()
    return svc


class _Snap:
    source_version = 'v1'
    updated_at = 'v1'

    def __init__(self, rows):
        self.rows = rows


def test_write_then_open_downloads_to_ephemeral_disk_once(blobs, monkeypatch):
    # Make the in-memory fake behave like azure's client for streams: upload_blob
    # accepts a file object, download_blob() returns a downloader with readinto().
    cls = blobs.get_blob_client('x', 'y').__class__
    original_upload = cls.upload_blob

    def upload(self, data, overwrite=True, content_settings=None):
        original_upload(self, data.read() if hasattr(data, 'read') else data, overwrite, content_settings)

    class _Dl:
        def __init__(self, data): self._d = data
        def readall(self): return self._d
        def readinto(self, fh): fh.write(self._d)

    def download(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Dl(self._store[self._key])

    monkeypatch.setattr(cls, 'upload_blob', upload)
    monkeypatch.setattr(cls, 'download_blob', download)
    assert search_db.write_for_snapshot('lib', _Snap([_big_row()])) is True
    assert search_db.is_current('lib', 'v1') and not search_db.is_current('lib', 'v2')
    db1 = search_db.open_database('lib')
    assert db1 is not None and db1.row_count() == 1
    assert search_db.open_database('lib') is db1  # second call reuses the local copy
    assert str(db1.path).startswith(search_db.SEARCH_DB_DIR)


def test_open_returns_none_when_no_current_database(blobs):
    assert search_db.open_database('nobody') is None


def test_searchdb_blobs_bypass_the_azure_files_share(blobs, monkeypatch, tmp_path):
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_DIR', str(tmp_path / 'share'))
    assert not isinstance(storage_utils._get_blob_client('lexical-index', 'k-searchdb.sqlite.gz'), storage_utils._ShareBackedBlob)
    assert isinstance(storage_utils._get_blob_client('lexical-index', 'k-sort.json.gz'), storage_utils._ShareBackedBlob)


def test_ensure_user_search_db_runs_the_streaming_build_only_when_stale(blobs, monkeypatch):
    cls = blobs.get_blob_client('x', 'y').__class__
    original = cls.upload_blob
    monkeypatch.setattr(cls, 'upload_blob', lambda self, data, overwrite=True, content_settings=None:
                        original(self, data.read() if hasattr(data, 'read') else data, overwrite, content_settings))
    calls = []

    class _V7(_Snap):
        source_version = 'v7'
        updated_at = 'v7'

    def hook(user_id, source_version=None):
        calls.append(user_id)
        blobs.blobs['lexical-index/' + storage_utils._lexical_index_manifest_blob_name(user_id)] = json.dumps(
            {'sourceVersion': 'v7'}).encode()
        search_db.write_for_snapshot(user_id, _V7([_big_row()]))
        return 1

    monkeypatch.setattr(storage_utils, 'LEXICAL_BUILD_HOOK', hook)
    assert storage_utils.ensure_user_search_db('lib') is True and calls == ['lib']   # stale -> built
    assert storage_utils.ensure_user_search_db('lib') is True and calls == ['lib']   # current -> no rebuild
    assert storage_utils.ensure_user_search_db('') is False


# --- the route ---------------------------------------------------------------------

def _route_ctx(monkeypatch, db, metadata=None):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({'p1': 'Alice'}, {'alice': ['p1']}))
    monkeypatch.setattr(app, '_expand_tokens_with_tag_embeddings', lambda tokens, uid: None)
    monkeypatch.setattr(app, '_get_metadata_entity',
                        lambda uid, name: (metadata or {}).get(name) or {'RowKey': name, 'rating': 3})
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: db)


def _search(query):
    with app.app.test_request_context(f'/photos/search?q={query}'):
        response = search_photos()
    return response.get_json() if hasattr(response, 'get_json') else response[0].get_json()


def test_route_returns_ranked_results_with_full_page_metadata(monkeypatch, db):
    _route_ctx(monkeypatch, db)
    payload = _search('dog')
    assert {p['filename'] for p in payload['photos']} == {'IMG_1.jpg', 'IMG_3.jpg'}
    assert payload['total'] == 2 and all(p['rating'] == 3 for p in payload['photos'])  # from the full row, not the reduced one


def test_route_filters_by_named_person_and_location(monkeypatch, db):
    _route_ctx(monkeypatch, db)
    assert [p['filename'] for p in _search('alice')['photos']] == ['IMG_1.jpg']
    payload = _search('photos in paris')
    assert [p['filename'] for p in payload['photos']] == ['IMG_1.jpg'] and payload['matchedLocations']


def test_route_year_in_query_narrows_to_that_year(monkeypatch, db):
    _route_ctx(monkeypatch, db)
    payload = _search('dog 2022')
    assert payload.get('matchedYear') == 2022
    assert all(p['filename'] != 'IMG_1.jpg' for p in payload['photos'])


def test_route_without_database_asks_tools_to_build_and_reports_it(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    nudged = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: nudged.append(uid))
    payload = _search('dog')
    assert payload == {'photos': [], 'total': 0, 'searchIndexBuilding': True} and nudged == ['owner']


def test_route_never_loads_the_lexical_index_or_scans_the_table(monkeypatch, db):
    _route_ctx(monkeypatch, db)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError('search must not load the library into memory'))
    monkeypatch.setattr(app, 'get_user_lexical_index', boom)
    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', boom)
    monkeypatch.setattr(app, 'vector_search_candidates', boom)
    assert _search('dog')['total'] == 2


def test_empty_query_short_circuits(monkeypatch):
    with app.app.test_request_context('/photos/search?q='):
        assert search_photos().get_json() == {'photos': [], 'total': 0}


# --- smart albums read from the database, same groups as from the table ------------------

def _group_view(candidates):
    return {c['name']: sorted(c['filenames']) for c in candidates}


@pytest.mark.parametrize('rule', ['recent-upload', 'event-window', 'location', 'person', 'tag-object'])
def test_smart_album_groups_from_the_database_match_the_table_scan(tmp_path, monkeypatch, rule):
    rows = [
        {'RowKey': 'a.jpg', 'locationCity': 'Paris', 'locationCountry': 'France', 'uploadDate': '2020-05-01T10:00:00+00:00',
         'peopleIds': json.dumps(['p1']), 'tags': json.dumps(['dog']), 'subjectTags': json.dumps(['dog']),
         'exifData': json.dumps({'DateTimeOriginal': '2019:12:25 09:00:00'})},
        {'RowKey': 'b.jpg', 'locationCity': 'Paris', 'locationCountry': 'France', 'uploadDate': '2020-05-01T11:00:00+00:00',
         'peopleIds': json.dumps(['p1', 'p2']), 'tags': json.dumps(['dog', 'ball']), 'subjectTags': json.dumps(['dog']),
         'exifData': json.dumps({'DateTimeOriginal': '2019:12:25 18:00:00'})},
        {'RowKey': 'c.jpg', 'locationCity': 'Goa', 'uploadDate': '2021-02-03T00:00:00+00:00', 'tags': json.dumps(['beach'])},
    ]
    monkeypatch.setattr(app, '_smart_album_person_names', lambda uid: {'p1': 'Asha', 'p2': 'Ravi'})
    path = str(tmp_path / 's.sqlite')
    search_db.build_database(rows, path)
    from_db = app._smart_album_candidates('u', rule, list(search_db.SearchDatabase(path).iter_smart_rows()))
    from_table = app._smart_album_candidates('u', rule, rows)
    assert _group_view(from_db) == _group_view(from_table)
    assert [c['name'] for c in from_db] == [c['name'] for c in from_table]
