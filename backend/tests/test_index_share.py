"""Index files on the shared Azure Files volume (_ShareBackedBlob): write-through,
ETag-validated read-through, session-start warm-up."""
from __future__ import annotations

import pytest

import storage_utils


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
        data = data.read() if hasattr(data, 'read') else data
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
    assert len(first) == 9 and set(first.values()) == {'filled'}
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


def test_upload_file_streams_to_blob_and_mirrors_to_share(share, tmp_path):
    src = tmp_path / 'built.json.gz'
    src.write_bytes(b'z' * 200)
    client = storage_utils._get_blob_client('lexical-index', 'lib-sort.json.gz')
    storage_utils._upload_file_to_blob(client, str(src), overwrite=True)
    assert _EtagBlob.store['lexical-index/lib-sort.json.gz'][0] == b'z' * 200
    assert client.download_blob().readall() == b'z' * 200
    assert share.clients['lexical-index/lib-sort.json.gz'].downloads == 0
    dest = tmp_path / 'fetched'
    client.fetch_to(str(dest))
    assert dest.read_bytes() == b'z' * 200
