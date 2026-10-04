"""The streaming library build: one paged pass feeding the listing blob, SQLite
search database, Explore and timeline aggregators -- with flat memory.

Regression for the index-build OOM: the old build held the whole library several
times over (full rows, a serialized copy, a listing copy, per-consumer reloads)."""
from __future__ import annotations

import gc
import gzip
import json
import tracemalloc

import pytest

import app
import search_db
import storage_utils
from tests.test_lexical_index import _FakeBlobServiceClient


class _StreamingTable:
    """Metadata table whose rows are generated lazily -- the test harness itself
    never holds the library, so any growth measured is the build's."""

    def __init__(self, make_rows):
        self._make_rows = make_rows
        self.select_seen = None

    def query_entities(self, query_filter, select=None, **kwargs):
        self.select_seen = select
        return self._make_rows()


def _row(i: int, payload: int = 0) -> dict:
    return {
        'PartitionKey': 'lib', 'RowKey': f'img_{i}.jpg', 'processing_complete': True,
        'tags': json.dumps([f'tag{i % 50}', 'dog']), 'subjectTags': json.dumps([f'tag{i % 50}']),
        'locationCity': f'City{i % 40}', 'locationCountry': 'France', 'uploadDate': '2020-05-01T00:00:00+00:00',
        'peopleIds': json.dumps([f'p{i % 7}']), 'ocrText': 'hello world ' * 5,
        'exifData': json.dumps({'Model': 'Cam', 'DateTimeOriginal': '2020:05:01 10:00:00'}),
        'blobPadding': 'x' * payload,  # a heavy column that nothing downstream keeps
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    blobs = _FakeBlobServiceClient()
    cls = blobs.get_blob_client('x', 'y').__class__
    original = cls.upload_blob
    # real azure accepts file objects; the in-memory fake needs reading for it
    monkeypatch.setattr(cls, 'upload_blob', lambda self, data, overwrite=True, content_settings=None:
                        original(self, data.read() if hasattr(data, 'read') else data, overwrite, content_settings))

    class _Dl:
        def __init__(self, data): self._d = data
        def readall(self): return self._d
        def readinto(self, fh): fh.write(self._d)

    def download(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Dl(self._store[self._key])

    monkeypatch.setattr(cls, 'download_blob', download)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blobs)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    search_db._OPEN.clear()
    monkeypatch.setattr(search_db, 'SEARCH_DB_DIR', str(tmp_path / 'ephemeral'))
    monkeypatch.setattr(storage_utils, 'LEXICAL_BUILD_HOOK', app._streaming_lexical_build)
    stored = {}
    monkeypatch.setattr(app, 'store_explore_summary', lambda uid, payload: stored.__setitem__('explore', payload))
    monkeypatch.setattr(app, 'store_timeline_summary', lambda uid, payload: stored.__setitem__('timeline', payload))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: {'RowKey': name, 'thumbnail_status': 'done'})
    return blobs, stored


def _use_rows(monkeypatch, n, payload=0):
    table = _StreamingTable(lambda: (_row(i, payload) for i in range(n)))
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    return table


def test_one_pass_produces_every_artifact(env, monkeypatch):
    blobs, stored = env
    table = _use_rows(monkeypatch, 300)
    assert storage_utils.refresh_user_lexical_artifacts('lib', 'v9') == 300

    # server-side projection: embeddings & other heavy columns are never requested
    assert 'photoEmbedding' not in table.select_seen and 'semanticEmbedding' not in table.select_seen
    assert 'ocrText' in table.select_seen and 'subjectTags' in table.select_seen
    # lexical manifest = readiness / freshness marker, written last
    manifest = json.loads(blobs.blobs['lexical-index/' + storage_utils._lexical_index_manifest_blob_name('lib')])
    assert manifest['sourceVersion'] == 'v9' and manifest['rowCount'] == 300 and manifest['dirty'] is False
    # listing blob is valid gzip JSON with every row, narrowed to the listing columns
    listing = json.loads(gzip.decompress(blobs.blobs['lexical-index/' + storage_utils._listing_index_json_blob_name('lib')]))
    assert len(listing['rows']) == 300 and listing['sourceVersion'] == 'v9'
    assert 'ocrText' not in listing['rows'][0] and listing['rows'][0]['RowKey'] == 'img_0.jpg'
    # search database is current and queryable
    assert search_db.is_current('lib', 'v9')
    db = search_db.open_database('lib')
    assert db.row_count() == 300 and any(f == 'img_3.jpg' for f, _ in db.candidates(['tag3']))
    # explore + timeline derived in the same pass
    assert stored['timeline']['totalCount'] == 300
    assert stored['explore']['sourceVersion'] == 'v9'
    assert {t['label'].lower() for t in stored['explore']['things']} >= {'dog'}
    assert any('France' in p['label'] for p in stored['explore']['places'])


def test_failed_scan_publishes_nothing_and_keeps_the_previous_good_build(env, monkeypatch):
    blobs, stored = env
    _use_rows(monkeypatch, 50)
    assert storage_utils.refresh_user_lexical_artifacts('lib', 'v1') == 50
    before = dict(blobs.blobs)

    def exploding():
        yield _row(0)
        raise RuntimeError('table storage hiccup mid-scan')

    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', _StreamingTable(exploding))
    assert storage_utils.refresh_user_lexical_artifacts('lib', 'v2') is None
    assert blobs.blobs == before  # an interrupted scan must never replace a good index with a partial one


def test_deleted_and_unfinished_rows_are_excluded(env, monkeypatch):
    rows = [_row(1), {**_row(2), 'processing_state': 'deleted'}, {**_row(3), 'RowKey': ''}]
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', _StreamingTable(lambda: iter(rows)))
    assert storage_utils.refresh_user_lexical_artifacts('lib', 'v1') == 1


def test_build_memory_does_not_scale_with_library_size(env, monkeypatch):
    """8k photos x ~12KB of heavy columns would be ~100MB if the build retained
    rows (as the old list()-based build did, several times over). Python-level
    peak must stay far below that -- sqlite pages and files are off-heap/on disk."""
    _use_rows(monkeypatch, 8000, payload=12000)
    gc.collect()
    tracemalloc.start()
    try:
        assert storage_utils.refresh_user_lexical_artifacts('lib', 'v1') == 8000
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 25 * 1024 * 1024, f'peak {peak / 1048576:.1f}MB -- the build is retaining rows'


def test_timeline_accumulator_matches_the_list_builder():
    import timeline_metadata
    rows = [_row(i) for i in range(10)] + [{'RowKey': 'undated.jpg'}]
    acc = timeline_metadata.TimelineAccumulator()
    for row in rows:
        acc.add(row)
    assert acc.summary() == timeline_metadata.build_timeline_summary(rows)
    assert acc.summary()['totalCount'] == 11 and acc.summary()['undatedCount'] == 1
