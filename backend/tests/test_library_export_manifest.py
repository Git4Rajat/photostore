"""Coverage for the client-orchestrated library export manifest endpoint
(/api/library/export/manifest -- backend.app.library_export_manifest_page),
which frontend/src/services/libraryExportDownloader.ts pages through instead
of the server building a zip (see /api/library/download/*, kept only as a
rollback path)."""
from __future__ import annotations

import re

import pytest

import app


class _FakePager:
    """Mimics azure.data.tables' TableEntityPropertiesPaged closely enough for
    this endpoint: one page per next(pager) call, continuation_token reflects
    what's left. Critically, the real SDK's continuation_token is a
    {'PartitionKey', 'RowKey'} DICT, not the plain string
    azure.core.paging.PageIterator's own type hint implies -- an earlier
    version of this fake used a bare stringified index here, which let
    _encode_export_manifest_cursor's real bug (calling .encode() on what's
    actually a dict) ship straight to photostore-test without this suite
    catching it. Encode the fake index inside the same dict shape so a
    regression back to assuming a string round-trips would fail here again."""

    def __init__(self, rows, page_size, continuation_token=None):
        self._rows = rows
        self._page_size = page_size
        self.continuation_token = continuation_token
        self._called = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._called and self.continuation_token is None:
            raise StopIteration
        self._called = True
        start = int((self.continuation_token or {}).get('RowKey') or 0)
        end = start + self._page_size
        page = self._rows[start:end]
        self.continuation_token = {'PartitionKey': 'fake', 'RowKey': str(end)} if end < len(self._rows) else None
        return iter(page)

    next = __next__


class _FakeItemPaged:
    def __init__(self, rows, page_size):
        self._rows = rows
        self._page_size = page_size

    def by_page(self, continuation_token=None):
        return _FakePager(self._rows, self._page_size, continuation_token)


class _FakeTable:
    """Just enough of azure.data.tables for the manifest endpoint: a
    partition-scoped, select-projected, page-at-a-time query_entities."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def add(self, partition_key, row_key, **fields):
        self.rows[(partition_key, row_key)] = {'PartitionKey': partition_key, 'RowKey': row_key, **fields}

    def query_entities(self, filter_str, select=None, results_per_page=None):
        m = re.match(r"PartitionKey eq '([^']*)'$", filter_str)
        assert m, f'unexpected filter: {filter_str}'
        pk = m.group(1)
        rows = [dict(row) for (p, _), row in sorted(self.rows.items()) if p == pk]
        if select is not None:
            rows = [{k: row[k] for k in select if k in row} for row in rows]
        return _FakeItemPaged(rows, results_per_page or max(len(rows), 1))


@pytest.fixture
def env(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_require_owner_context', lambda require_auth=True: ('owner', 'lib1', None))
    monkeypatch.setattr(app, 'resolve_physical_blob_name', lambda lib, fn, kind='image': fn)
    monkeypatch.setattr(
        app, '_stable_container_read_sas',
        lambda container: (f'https://fake.blob/{container}', 'sas=1', '2099-01-01T00:00:00+00:00'),
    )
    return table


def _get_manifest(query_string: str = ''):
    path = '/api/library/export/manifest' + (f'?{query_string}' if query_string else '')
    with app.app.test_request_context(path):
        return app.library_export_manifest_page()


def test_manifest_pages_through_a_large_library(env):
    for i in range(1250):
        env.add('lib1', f'photo-{i:05d}.jpg', processing_state='done')

    seen = []
    cursor = None
    pages = 0
    while True:
        qs = 'pageSize=500' + (f'&cursor={cursor}' if cursor else '')
        response = _get_manifest(qs)
        body = response.get_json()
        seen.extend(f['filename'] for f in body['files'])
        pages += 1
        cursor = body.get('nextCursor')
        if not cursor:
            break

    assert pages == 3  # 500 + 500 + 250
    assert seen == [f'photo-{i:05d}.jpg' for i in range(1250)]
    assert len(set(seen)) == 1250


def test_manifest_skips_deleted_rows_and_shares_one_sas_for_the_page(env):
    env.add('lib1', 'a.jpg', processing_state='done')
    env.add('lib1', 'b.jpg', processing_state='deleted')
    env.add('lib1', 'c.jpg', processing_state='done')

    body = _get_manifest().get_json()

    assert [f['filename'] for f in body['files']] == ['a.jpg', 'c.jpg']
    assert [f['blobName'] for f in body['files']] == ['a.jpg', 'c.jpg']
    assert body['nextCursor'] is None
    # One shared token for the whole page, not one per file -- the client
    # builds each file's URL itself as f'{baseUrl}/{blobName}?{sas}'.
    assert body['baseUrl'] == f'https://fake.blob/{app.BLOB_IMAGE_CONTAINER}'
    assert body['sas'] == 'sas=1'


def test_manifest_empty_library_still_returns_a_shared_sas(env):
    body = _get_manifest().get_json()
    assert body == {
        'baseUrl': f'https://fake.blob/{app.BLOB_IMAGE_CONTAINER}',
        'sas': 'sas=1',
        'files': [],
        'nextCursor': None,
    }


def test_manifest_rejects_non_owner(monkeypatch):
    monkeypatch.setattr(
        app, '_require_owner_context',
        lambda require_auth=True: (None, None, (app.jsonify({'error': 'Only the library owner can do that.'}), 403)),
    )
    response = _get_manifest()
    assert response[1] == 403


def test_manifest_rejects_invalid_cursor(env):
    response = _get_manifest('cursor=not-valid-base64!!!')
    assert response[1] == 400
