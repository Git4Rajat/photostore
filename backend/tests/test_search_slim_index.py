"""Slim browser search index (storage_utils._search_slim_row & friends), the
INDEX_DISK_CACHE_DIR ETag cache, and search-index route blob selection."""
from __future__ import annotations

import gzip
import json

import pytest

import app
import storage_utils
from routes.photos import photos_search_index
from tests.test_lexical_index import _FakeBlobServiceClient, _seed_row
from tests.fakes import FakeTable


def _big_row():
    return {
        'PartitionKey': 'lib', 'RowKey': 'a.jpg',
        'tags': json.dumps(['dog', 'grass', 'blurry']), 'subjectTags': json.dumps(['dog']),
        'objects': json.dumps(['dog', 'ball']), 'backgroundTags': json.dumps(['grass']),
        'tagMetadata': json.dumps([
            {'tag': 'dog', 'source': 'ai_tag', 'confidence': 0.9},
            {'tag': 'grass', 'source': 'ai_tag', 'confidence': 0.6},
            {'tag': 'blurry', 'source': 'ai_tag', 'confidence': 0.26},
            {'tag': 'mine', 'source': 'user', 'confidence': 0.1},
        ]),
        'caption': 'a dog', 'locationCity': 'Paris', 'faceCount': 1,
        'latitude': '48.856614', 'longitude': '2.352222',
        'ocrText': ('word  ' * 5000), 'weakTags': '["w"]', 'faces': 'f' * 5000,
        'uploadDate': '', 'upload_started_at': '2020-01-02T00:00:00+00:00',
        'exifData': json.dumps({'Model': 'Cam', 'DateTimeOriginal': '2020:01:01 10:00:00',
                                'MakerNote': 'z' * 5000, 'GPS.GPSLatitude': '1'}),
        'processing_metadata': json.dumps({
            'client_ai_vision': {'predictions': [{'label': 'cat', 'score': 0.9}, {'label': 'dog', 'score': 0.95},
                                                 {'label': 'mid', 'score': 0.3}, {'label': 'low', 'score': 0.05}]},
            'client_face': {'blob': 'y' * 5000},
        }),
    }


def test_slim_row_dedupes_filters_low_confidence_and_collapses_fields():
    slim = storage_utils._search_slim_row(_big_row())
    assert slim['subjectTags'] == ['dog']
    # grass kept (0.6), 'blurry' dropped (0.26 < 0.45), objects' 'ball' has no recorded confidence -> kept;
    # tags are real arrays, deduped across tags/objects/backgroundTags, subjects not repeated.
    assert slim['tags'] == ['grass', 'ball']
    assert slim['predictionLabels'] == ['cat']  # 'dog' already a tag; 0.3/0.05 below cutoff
    assert 'objects' not in slim and 'backgroundTags' not in slim and 'tagMetadata' not in slim
    assert slim['latitude'] == slim['longitude'] == '1'
    assert slim['uploadDate'] == '2020-01-02T00:00:00+00:00' and 'upload_started_at' not in slim
    assert len(slim['ocrText']) == storage_utils.SEARCH_INDEX_OCR_MAX_CHARS
    assert json.loads(slim['exifData']) == {'Model': 'Cam', 'DateTimeOriginal': '2020:01:01 10:00:00', 'GPSInfo': '1'}
    for dropped in ('weakTags', 'faces', 'PartitionKey', 'processing_metadata'):
        assert dropped not in slim
    assert len(json.dumps(slim)) < len(json.dumps(_big_row())) / 6


def test_slim_row_keeps_user_tags_regardless_of_confidence():
    row = {'RowKey': 'b.jpg', 'tags': json.dumps(['mine']),
           'tagMetadata': json.dumps([{'tag': 'mine', 'source': 'user', 'confidence': 0.01}])}
    assert storage_utils._search_slim_row(row)['tags'] == ['mine']


def test_slim_row_caps_tag_count(monkeypatch):
    monkeypatch.setattr(storage_utils, 'SEARCH_INDEX_MAX_TAGS', 3)
    row = {'RowKey': 'c.jpg', 'subjectTags': json.dumps(['a']), 'tags': json.dumps(['b', 'c', 'd', 'e'])}
    slim = storage_utils._search_slim_row(row)
    assert slim['subjectTags'] + slim['tags'] == ['a', 'b', 'c']


@pytest.fixture
def ctx(monkeypatch):
    table = FakeTable()
    blobs = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blobs)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    return table, blobs


def test_refresh_writes_slim_blob_and_manifest_matching_lexical(ctx):
    table, blobs = ctx
    table.upsert_entity({**_big_row(), 'processing_complete': True})
    snap = storage_utils.refresh_user_lexical_index('lib', source_version='v1')
    manifest = storage_utils.load_search_index_manifest('lib')
    assert manifest['sourceVersion'] == snap.source_version == 'v1'
    _, name = storage_utils.get_search_index_blob_location('lib')
    parsed = json.loads(gzip.decompress(blobs.blobs[f'lexical-index/{name}']))
    assert parsed['rowCount'] == 1 and 'faces' not in parsed['rows'][0]
    full = gzip.decompress(blobs.blobs[f'lexical-index/{storage_utils._lexical_index_json_blob_name("lib")}'])
    assert len(gzip.decompress(blobs.blobs[f'lexical-index/{name}'])) < len(full)


def test_delete_removes_slim_blobs(ctx):
    table, blobs = ctx
    table.upsert_entity({**_big_row(), 'processing_complete': True})
    storage_utils.refresh_user_lexical_index('lib', source_version='v1')
    storage_utils.delete_user_lexical_index_data('lib')
    assert not [k for k in blobs.blobs if 'search' in k]


def test_route_serves_slim_only_when_current(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary',
                        lambda uid, kind: {'source_version': 'v9', 'updated_at': 'u', 'dirty': False})
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda c, b: (f'https://x/{b}', 'exp'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: None)
    for version, expect_available in (('v9', True), ('v1', False)):
        monkeypatch.setattr(storage_utils, 'load_search_index_manifest',
                            lambda uid, v=version: {'sourceVersion': v, 'schemaVersion': storage_utils._SEARCH_INDEX_SCHEMA_VERSION})
        with app.app.test_request_context('/api/photos/search-index'):
            body = photos_search_index().get_json()
        assert body['available'] is expect_available
        if expect_available:
            assert body['indexUrl'].endswith('-search.json.gz')


def test_ensure_slim_derives_from_existing_lexical_blob(ctx):
    table, blobs = ctx
    table.upsert_entity({**_big_row(), 'processing_complete': True})
    storage_utils.refresh_user_lexical_index('lib', source_version='v1')
    # simulate a pre-deploy library: slim blob missing
    for k in [k for k in blobs.blobs if 'search' in k]:
        del blobs.blobs[k]
    assert storage_utils.ensure_user_search_slim_index('lib') is True
    assert storage_utils.load_search_index_manifest('lib')['schemaVersion'] == storage_utils._SEARCH_INDEX_SCHEMA_VERSION


class _EtagBlob:
    def __init__(self, data, etag):
        self.data, self.etag, self.downloads = data, etag, 0

    def get_blob_properties(self):
        return type('P', (), {'etag': self.etag})()

    def download_blob(self):
        self.downloads += 1
        return type('D', (), {'readall': lambda _s, d=self.data: d})()


def test_disk_cache_hits_on_same_etag_and_refetches_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_DIR', str(tmp_path))
    blob = _EtagBlob(b'one', '"e1"')
    assert storage_utils._download_with_disk_cache('sort', 'lib', blob) == b'one'
    assert storage_utils._download_with_disk_cache('sort', 'lib', blob) == b'one'
    assert blob.downloads == 1
    blob.data, blob.etag = b'two', '"e2"'
    assert storage_utils._download_with_disk_cache('sort', 'lib', blob) == b'two'
    assert blob.downloads == 2


def test_disk_cache_disabled_by_default(monkeypatch):
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_DIR', '')
    blob = _EtagBlob(b'x', '"e"')
    storage_utils._download_with_disk_cache('sort', 'lib', blob)
    storage_utils._download_with_disk_cache('sort', 'lib', blob)
    assert blob.downloads == 2


def test_timeline_route_serves_precomputed_blob_without_touching_listing_index(monkeypatch):
    from routes.photos import photos_timeline
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not load listing rows')))
    monkeypatch.setattr(storage_utils, 'load_timeline_summary', lambda uid: {'totalCount': 7, 'years': {}})
    with app.app.test_request_context('/api/photos/timeline'):
        assert photos_timeline().get_json()['totalCount'] == 7


def test_timeline_route_cold_returns_empty_and_nudges_tools(monkeypatch):
    from routes.photos import photos_timeline
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(storage_utils, 'load_timeline_summary', lambda uid: None)
    nudged = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: nudged.append(uid))
    with app.app.test_request_context('/api/photos/timeline'):
        body = photos_timeline().get_json()
    assert body['totalCount'] == 0 and nudged == ['owner']


def test_refresh_timeline_summary_stores_blob(ctx, monkeypatch):
    table, _ = ctx
    table.upsert_entity({**_big_row(), 'processing_complete': True, 'uploadDate': '2020-01-01T00:00:00+00:00'})
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user',
                        lambda uid, purpose, **k: [{'RowKey': 'a.jpg', 'uploadDate': '2020-01-01T00:00:00+00:00'}])
    app.refresh_user_timeline_summary('lib')
    assert storage_utils.load_timeline_summary('lib')['totalCount'] == 1
