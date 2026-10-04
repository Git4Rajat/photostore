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
        'tags': '["dog"]', 'subjectTags': '["dog"]', 'caption': 'a dog',
        'locationCity': 'Paris', 'faceCount': 1,
        'ocrText': 'x' * 10000,
        'exifData': json.dumps({'Model': 'Cam', 'DateTimeOriginal': '2020:01:01 10:00:00',
                                'MakerNote': 'z' * 5000, 'GPS.GPSLatitude': '1'}),
        'processing_metadata': json.dumps({
            'client_ai_vision': {'predictions': [{'label': 'cat', 'score': 0.9, 'extra': 'q'},
                                                 {'label': 'low', 'score': 0.05}]},
            'client_face': {'blob': 'y' * 5000},
        }),
        'tagMetadata': 'm' * 5000, 'weakTags': '["w"]', 'faces': 'f' * 5000,
    }


def test_slim_row_keeps_search_fields_and_drops_bulk():
    slim = storage_utils._search_slim_row(_big_row())
    assert slim['RowKey'] == 'a.jpg' and slim['caption'] == 'a dog' and slim['locationCity'] == 'Paris'
    assert len(slim['ocrText']) == storage_utils.SEARCH_INDEX_OCR_MAX_CHARS
    exif = json.loads(slim['exifData'])
    assert exif == {'Model': 'Cam', 'DateTimeOriginal': '2020:01:01 10:00:00', 'GPSInfo': '1'}
    preds = json.loads(slim['processing_metadata'])['client_ai_vision']['predictions']
    assert [p['label'] for p in preds] == ['cat']
    for dropped in ('tagMetadata', 'weakTags', 'faces', 'PartitionKey'):
        assert dropped not in slim
    assert len(json.dumps(slim)) < len(json.dumps(_big_row())) / 4


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


def test_route_serves_slim_only_when_in_lockstep(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary',
                        lambda uid, kind: {'source_version': 'v9', 'updated_at': 'u', 'dirty': False})
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda c, b: (f'https://x/{b}', 'exp'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)
    for version, expect in (('v9', '-search.json.gz'), ('v1', '.json.gz')):
        monkeypatch.setattr(storage_utils, 'load_search_index_manifest', lambda uid, v=version: {'sourceVersion': v})
        with app.app.test_request_context('/api/photos/search-index'):
            url = photos_search_index().get_json()['indexUrl']
        assert url.endswith(expect)
        assert url.endswith('-search.json.gz') == (version == 'v9')


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
