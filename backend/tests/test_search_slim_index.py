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


class _Props:
    def __init__(self, etag, size):
        self.etag, self.size = etag, size


class _Down:
    def __init__(self, data, etag):
        self._d, self.properties = data, _Props(etag, len(data))

    def readall(self):
        return self._d

    def readinto(self, fh):
        fh.write(self._d)


class _EtagBlob:
    """Minimal azure BlobClient stand-in with ETags."""
    store: dict = {}

    def __init__(self, name):
        self.name, self.downloads = name, 0

    def upload_blob(self, data, overwrite=True, content_settings=None):
        _EtagBlob.store[self.name] = (bytes(data), f'"e{len(_EtagBlob.store)}-{len(data)}"')
        return {'etag': _EtagBlob.store[self.name][1]}

    def get_blob_properties(self):
        data, etag = _EtagBlob.store[self.name]
        return _Props(etag, len(data))

    def download_blob(self, *a, **k):
        self.downloads += 1
        data, etag = _EtagBlob.store[self.name]
        return _Down(data, etag)

    def delete_blob(self):
        _EtagBlob.store.pop(self.name, None)


class _EtagService:
    def __init__(self):
        self.clients = {}

    def get_blob_client(self, container, blob):
        return self.clients.setdefault(f'{container}/{blob}', _EtagBlob(f'{container}/{blob}'))


@pytest.fixture
def share(tmp_path, monkeypatch):
    _EtagBlob.store = {}
    svc = _EtagService()
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', svc)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_MIN_BYTES', 10)
    return svc


def test_upload_writes_through_and_next_read_is_served_from_share(share):
    client = storage_utils._get_blob_client('lexical-index', 'lib-sort.json.gz')
    client.upload_blob(b'x' * 100, overwrite=True)
    assert client.download_blob().readall() == b'x' * 100
    assert share.clients['lexical-index/lib-sort.json.gz'].downloads == 0  # never touched blob data


def test_changed_etag_invalidates_share_copy(share):
    client = storage_utils._get_blob_client('lexical-index', 'lib-sort.json.gz')
    client.upload_blob(b'a' * 100)
    # another writer replaces the blob behind the share's back (no write-through)
    share.clients['lexical-index/lib-sort.json.gz'].upload_blob(b'b' * 120)
    assert client.download_blob().readall() == b'b' * 120
    assert share.clients['lexical-index/lib-sort.json.gz'].downloads == 1


def test_small_blobs_and_non_index_containers_bypass_share(share, tmp_path):
    client = storage_utils._get_blob_client('lexical-index', 'lib.json')  # manifest-sized
    client.upload_blob(b'tiny')
    assert not list(tmp_path.rglob('lib.json*'))
    other = storage_utils._get_blob_client('images', 'photo.jpg')
    assert not isinstance(other, storage_utils._ShareBackedBlob)


def test_delete_removes_share_files(share, tmp_path):
    client = storage_utils._get_blob_client('lexical-index', 'lib-sort.json.gz')
    client.upload_blob(b'x' * 100)
    client.delete_blob()
    assert not list(tmp_path.rglob('lib-sort*'))


def test_disabled_without_cache_dir(monkeypatch):
    monkeypatch.setattr(storage_utils, 'INDEX_DISK_CACHE_DIR', '')
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', _EtagService())
    assert not isinstance(storage_utils._get_blob_client('lexical-index', 'x'), storage_utils._ShareBackedBlob)


def test_warm_fills_share_for_every_index_kind_and_second_run_hits(share, tmp_path, monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'blob_vector_index_container', 'vector-index')
    for container, blob in storage_utils._user_index_blob_locations('lib'):
        # seed straight into "blob storage" (bypassing write-through)
        share.get_blob_client(container, blob).upload_blob(b'y' * 100)
    first = storage_utils.warm_user_index_files('lib')
    assert len(first) == 10 and set(first.values()) == {'filled'}
    second = storage_utils.warm_user_index_files('lib')
    assert set(second.values()) == {'hit'}
    client = storage_utils._get_blob_client('lexical-index', storage_utils._sort_index_json_blob_name('lib'))
    assert client.download_blob().readall() == b'y' * 100
    assert share.clients[f'lexical-index/{storage_utils._sort_index_json_blob_name("lib")}'].downloads == 1  # only the warm fill


def test_warm_async_is_single_flight_with_cooldown(share, monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'warm_user_index_files', lambda uid: calls.append(uid))
    storage_utils._WARM_LAST.clear()
    assert storage_utils.warm_user_index_files_async('lib') is True
    assert storage_utils.warm_user_index_files_async('lib') is False
