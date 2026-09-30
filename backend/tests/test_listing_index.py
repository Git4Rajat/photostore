"""Coverage for the "listing" search-index projection (storage_utils.
get_user_listing_index): a narrower blob containing only
PHOTO_LIST_SELECT_FIELDS, derived as a side effect of refresh_user_lexical_index
so plain gallery/timeline browsing never pays for ocrText/tagMetadata/
weakTags/objects/faces/processing_metadata the way reading the full lexical
index directly used to.
"""
from __future__ import annotations

import threading
import time

import pytest

import storage_utils
from tests.fakes import FakeTable


class _FakeBlob:
    def __init__(self, store: dict, key: str) -> None:
        self._store = store
        self._key = key

    def upload_blob(self, data, overwrite=True, content_settings=None):
        self._store[self._key] = data

    def download_blob(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Downloaded(self._store[self._key])

    def delete_blob(self):
        self._store.pop(self._key, None)


class _Downloaded:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _FakeBlobServiceClient:
    def __init__(self) -> None:
        self.blobs: dict = {}

    def get_blob_client(self, container, blob):
        return _FakeBlob(self.blobs, f'{container}/{blob}')


def _seed_row(table, user_id: str, filename: str, **overrides) -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': filename, 'processing_complete': True, **overrides})


@pytest.fixture
def ctx(monkeypatch):
    metadata = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    yield metadata, blob_service


def test_listing_index_excludes_search_only_fields(ctx):
    metadata, _blobs = ctx
    _seed_row(
        metadata, 'u1', 'a.jpg',
        tags='["dog"]', rating=5, ocrText='some receipt text', tagMetadata='{"dog": 0.9}',
        weakTags='["animal"]', objects='["dog"]', faces='[]', processing_metadata='{"face": {}}',
    )

    storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    listing = storage_utils.get_user_listing_index('u1', allow_refresh=False)

    assert listing is not None
    row = listing['rows'][0]
    assert row['RowKey'] == 'a.jpg'
    assert row['tags'] == '["dog"]'
    assert row['rating'] == 5
    for heavy_field in ('ocrText', 'tagMetadata', 'weakTags', 'objects', 'faces'):
        assert heavy_field not in row


def test_listing_index_keeps_processing_metadata_for_status_badges(ctx):
    """processing_metadata IS in PHOTO_LIST_SELECT_FIELDS (status badges read
    it) -- must survive the listing projection unlike the other heavy fields."""
    metadata, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]', processing_metadata='{"face": {"count": 2}}')

    storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    listing = storage_utils.get_user_listing_index('u1', allow_refresh=False)

    assert listing['rows'][0]['processing_metadata'] == '{"face": {"count": 2}}'


def test_listing_index_written_as_side_effect_not_separate_rebuild(ctx):
    """get_user_listing_index(allow_refresh=False) must see data purely from
    refresh_user_lexical_index's side-effect write -- no separate rebuild
    trigger of its own."""
    metadata, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')

    # Before any lexical refresh has ever run, there's nothing to serve.
    assert storage_utils.get_user_listing_index('u1', allow_refresh=False) is None

    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    listing = storage_utils.get_user_listing_index('u1', allow_refresh=False)
    assert listing is not None
    assert [r['RowKey'] for r in listing['rows']] == ['a.jpg']


def test_listing_index_falls_back_to_lexical_index_when_stale(ctx):
    """If the listing blob is missing/stale but allow_refresh=True, it must
    fall back through get_user_lexical_index (which will itself rebuild and
    re-derive the listing blob), not just return None."""
    metadata, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='["cat"]', ocrText='ignored by listing')

    listing = storage_utils.get_user_listing_index('u1', allow_refresh=True)

    assert listing is not None
    assert listing['rows'][0]['RowKey'] == 'a.jpg'
    assert 'ocrText' not in listing['rows'][0]


def test_listing_index_reflects_incremental_merge_updates(ctx):
    """An incremental (non-full) lexical rebuild must still refresh the
    listing projection for the changed filename."""
    metadata, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='["cat"]', rating=1)
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'a.jpg', 'tags': '["cat"]', 'rating': 5, 'processing_complete': True})
    storage_utils.touch_user_search_indexes_state('u1', filenames='a.jpg')
    storage_utils.refresh_user_lexical_index('u1', source_version='v2')

    listing = storage_utils.get_user_listing_index('u1', allow_refresh=False)
    assert listing['rows'][0]['rating'] == 5


def test_listing_index_allow_sync_build_false_never_blocks_on_cold_account(ctx, monkeypatch):
    """Regression pin: get_user_listing_index's fallback to
    get_user_lexical_index used to hardcode allow_refresh=True with no
    allow_sync_build plumbing at all, so a caller passing
    allow_sync_build=False (e.g. /photos/timeline via
    _cached_metadata_list_rows_for_user) still blocked on the lexical
    index's own default (blocking) cold-build path -- defeating the whole
    point of the non-blocking call. On a genuinely cold account (no listing
    blob, no lexical snapshot ever built), this must return None immediately
    instead of blocking on the ~60-75s full scan."""
    metadata, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='["cat"]')

    entered = threading.Event()
    release = threading.Event()

    def slow_build(user_id, source_version):
        entered.set()
        release.wait(timeout=5)
        return storage_utils.LexicalIndexSnapshot(
            user_id=user_id, source_version=source_version,
            schema_version=storage_utils._LEXICAL_INDEX_SCHEMA_VERSION,
            updated_at=source_version, rows=[{'RowKey': 'a.jpg'}],
        )

    monkeypatch.setattr(storage_utils, '_build_user_lexical_index_snapshot', slow_build)

    start = time.monotonic()
    result = storage_utils.get_user_listing_index('u1', allow_refresh=True, allow_sync_build=False)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f'get_user_listing_index blocked for {elapsed:.2f}s on a cold account'
    assert result is None
    assert entered.wait(timeout=5), 'background lexical rebuild never started'

    release.set()
    lock = storage_utils._LEXICAL_INDEX_REBUILD_LOCKS.lock_for('u1')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background lexical index rebuild never completed')


def test_delete_user_lexical_index_data_also_clears_listing_blob(ctx):
    metadata, blob_service = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    assert storage_utils.get_user_listing_index('u1', allow_refresh=False) is not None

    storage_utils.delete_user_lexical_index_data('u1')

    assert storage_utils.get_user_listing_index('u1', allow_refresh=False) is None
    listing_blobs = [k for k in blob_service.blobs if 'listing' in k]
    assert listing_blobs == []
