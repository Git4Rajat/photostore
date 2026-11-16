"""Download library's manifest, token and info endpoints (library_download.py,
docs/library-download-dedicated.md): one streamed pass that tells the browser where each file lives,
where to save it, and how to verify it."""
import json

import pytest

import app
import library_download
from fakes import FakeTable
from routes.library_operations import (library_download_info, library_download_manifest,
                                       library_download_token)

LIB = 'lib1'
SHA = 'a' * 64


def exif(date):
    return json.dumps({'DateTimeOriginal': date})


@pytest.fixture
def table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_require_owner_context', lambda require_auth=True: ('owner', LIB, None))
    return table


def add(table, name, lib=LIB, **fields):
    table.upsert_entity({'PartitionKey': lib, 'RowKey': name, **fields})


def manifest():
    with app.app.test_request_context('/api/library/download/manifest'):
        response = library_download_manifest()
        if isinstance(response, tuple):
            return response
        return [json.loads(line) for line in response.get_data(as_text=True).splitlines()]


def test_manifest_lists_every_file_with_place_size_and_hash(table):
    add(table, 'IMG_1.HEIC', anonymousImageId='anon1', size=2_000_000, fileHash=SHA.upper(),
        exifData=exif('2024:05:17 10:00:00'), caption='Beach', rating=4, likedBy=json.dumps([LIB]),
        peopleIds=json.dumps(['p1']))
    add(table, 'old.jpg', size='300', uploadDate='2023-01-02T03:04:05+00:00')  # pre-anonymization row
    add(table, 'gone.jpg', anonymousImageId='x', processing_state='deleted')
    add(table, 'IMG_1.HEIC', lib='other', anonymousImageId='theirs', size=9)

    *files, end = manifest()

    assert files == [
        {'name': 'IMG_1.HEIC', 'blob': 'anon1', 'path': '2024/05/IMG_1.HEIC', 'size': 2_000_000, 'sha256': SHA,
         'caption': 'Beach', 'rating': 4, 'liked': True, 'people': ['p1']},
        {'name': 'old.jpg', 'blob': 'old.jpg', 'path': '2023/01/old.jpg', 'size': 300},
    ]
    assert end == {'end': True, 'files': 2, 'bytes': 2_000_300, 'unsized': 0}


def test_live_photo_clip_is_filed_beside_its_still(table):
    add(table, 'IMG_2.HEIC', anonymousImageId='s', exifData=exif('2024:12:31 23:59:00'))
    add(table, 'IMG_2.MOV', anonymousImageId='c', livePhotoOf='IMG_2.HEIC', exifData=exif('2025:01:01 00:00:01'))

    still, clip, _ = manifest()

    assert still['path'] == '2024/12/IMG_2.HEIC'
    assert clip['path'] == '2024/12/IMG_2.MOV' and clip['livePhotoOf'] == 'IMG_2.HEIC'


def test_unsafe_and_colliding_names_get_distinct_safe_paths(table):
    for name in ('a:b.jpg', 'a_b.jpg', 'A_B.JPG', 'CON.jpg', 'trail. '):
        add(table, name, anonymousImageId=name, uploadDate='2024-03-01T00:00:00+00:00')

    *files, _ = manifest()

    paths = [f['path'] for f in files]
    assert len({p.lower() for p in paths}) == len(paths)
    assert all(not set(p.split('/')[-1]) & set('<>:"\\|?*') for p in paths)
    assert {f['name']: f['path'] for f in files}['CON.jpg'] == '2024/03/_CON.jpg'
    assert {f['name'] for f in files} == {'a:b.jpg', 'a_b.jpg', 'A_B.JPG', 'CON.jpg', 'trail. '}


def test_rows_without_size_or_date_are_still_listed(table):
    add(table, 'mystery', anonymousImageId='m', fileHash='not-a-hash')

    entry, end = manifest()

    assert entry == {'name': 'mystery', 'blob': 'm', 'path': 'undated/mystery'}
    assert end['unsized'] == 1 and end['bytes'] == 0


def test_manifest_streams_a_large_library_in_chunks(table):
    for i in range(3000):
        add(table, f'p{i:05d}.jpg', anonymousImageId=f'a{i}', size=1000, uploadDate='2024-01-01T00:00:00+00:00')

    chunks = list(library_download.ndjson(library_download.entries(table, LIB)))

    assert 1 < len(chunks) < 30
    lines = b''.join(chunks).decode().splitlines()
    assert len(lines) == 3001 and json.loads(lines[-1])['bytes'] == 3_000_000


def test_storage_failure_is_a_503_not_a_truncated_success(table, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('table unavailable')
    monkeypatch.setattr(table, 'query_entities', broken)

    body, status = manifest()

    assert status == 503


def test_only_the_owner_gets_a_manifest_token_or_info(monkeypatch):
    denied = (app.jsonify, 403)
    monkeypatch.setattr(app, '_require_owner_context', lambda require_auth=True: (None, None, denied))
    with app.app.test_request_context('/'):
        assert library_download_manifest() is denied
        assert library_download_token() is denied
        assert library_download_info() is denied


def test_token_is_short_lived_and_not_cached(table, monkeypatch):
    seen = {}

    def sas(container, *, ttl_seconds=None):
        seen.update(container=container, ttl=ttl_seconds)
        return 'https://acct.blob/images', 'sig=1', '2026-11-16T12:00:00+00:00'
    monkeypatch.setattr(app, '_stable_container_read_sas', sas)

    with app.app.test_request_context('/api/library/download/token'):
        response = library_download_token()

    assert response.get_json() == {'baseUrl': 'https://acct.blob/images', 'sas': 'sig=1',
                                   'expiresAt': '2026-11-16T12:00:00+00:00'}
    assert seen == {'container': app.BLOB_IMAGE_CONTAINER, 'ttl': 12 * 3600}
    assert response.headers['Cache-Control'] == 'no-store'


def test_info_lists_albums_with_photos_and_named_people(table, monkeypatch):
    albums, people = FakeTable(), FakeTable()
    monkeypatch.setattr(app, 'albums_table_client', albums)
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, 'library_store', None)
    import album_store
    album = {'PartitionKey': LIB, 'RowKey': 'al1', 'name': 'Trip'}
    album_store.write_filenames(album, ['IMG_1.HEIC', 'old.jpg'])
    albums.upsert_entity(album)
    albums.upsert_entity({'PartitionKey': 'other', 'RowKey': 'al2', 'name': 'Theirs'})
    people.upsert_entity({'PartitionKey': LIB, 'RowKey': 'p1', 'name': 'Asha'})
    people.upsert_entity({'PartitionKey': LIB, 'RowKey': 'p2', 'name': ''})

    with app.app.test_request_context('/api/library/download/info'):
        body = library_download_info().get_json()

    assert body == {'libraryName': '', 'albums': [{'id': 'al1', 'name': 'Trip', 'photos': ['IMG_1.HEIC', 'old.jpg']}],
                    'people': [{'id': 'p1', 'name': 'Asha'}]}
