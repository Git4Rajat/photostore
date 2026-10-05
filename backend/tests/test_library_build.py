"""Resumable chunked first build: search, sort and access indexes for a library with no database yet."""
from __future__ import annotations

import json
import re

import pytest

import index_files
import library_build
import search_db
import search_utils
import storage_utils
from tests.fakes import FakeTable
from tests.test_search_db_delta import _Svc
from tests.test_table_scan import _RangeTable


def _rows(n=95):
    out = []
    for i in range(n):
        out.append({'PartitionKey': 'lib', 'RowKey': f'IMG_{i:04d}.jpg', 'processing_complete': True,
                    'tags': json.dumps(['dog' if i % 2 else 'cat']), 'subjectTags': json.dumps(['dog' if i % 2 else 'cat']),
                    'uploadDate': f'2021-03-{(i % 27) + 1:02d}T10:00:00+00:00', 'rating': i % 5,
                    'anonymousImageId': f'blob-{i}', 'thumbnail_status': 'done'})
    out.append({'PartitionKey': 'lib', 'RowKey': 'IMG_trashed.jpg', 'processing_complete': True, 'processing_state': 'deleted',
                'tags': '[]', 'uploadDate': '2021-01-01T00:00:00+00:00'})
    out.append({'PartitionKey': 'lib', 'RowKey': 'IMG_pending.jpg', 'processing_complete': False, 'tags': '[]',
                'uploadDate': '2021-01-02T00:00:00+00:00'})
    return out


@pytest.fixture
def world(monkeypatch, tmp_path):
    svc, dirty = _Svc(), FakeTable()
    table = _RangeTable([])
    table.rows = sorted(_rows(), key=lambda r: r['RowKey'])
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', svc)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setattr(search_db, 'SEARCH_DB_DIR', str(tmp_path / 'ephemeral'))
    monkeypatch.setenv('INDEX_BUILD_SQLITE_DIR', str(tmp_path / 'sqlite'))
    monkeypatch.setattr(index_files, 'WORK_DIR', str(tmp_path / 'work'))
    monkeypatch.setattr(library_build, 'CHUNK_ROWS', 20)
    monkeypatch.setattr(search_db, 'DELTA_MAX_ROW_FRACTION', 100.0)
    monkeypatch.setattr(library_build, 'FINALIZE_HOOK', None)
    search_db._OPEN.clear()
    return svc, table


def _manifest(svc):
    return json.loads(svc.store['lexical-index/' + search_db._blob_names('lib')[1]])


def _names(db, query):
    return sorted(f for f, _ in db.candidates(search_db.match_terms(search_utils.parse_search_query(query))))


def _blob_rows(svc, blob):
    import gzip
    return [json.loads(line.decode().rstrip(',')) for line in gzip.decompress(svc.store['lexical-index/' + blob]).splitlines()[1:-1]]


def test_first_build_makes_all_three_indexes_from_one_scan(world):
    svc, table = world
    scans_before = len(table.calls)
    result = library_build.bootstrap_library_build('lib')
    assert result['status'] == 'built' and result['rows'] == 97 and result['chunks'] == 5   # ceil(97 / 20)
    assert len(table.calls) - scans_before == 1                                              # ONE pass for everything

    manifest = _manifest(svc)
    assert 'building' not in manifest and manifest['baseSeq'] == 5 and manifest['rowCount'] == 95
    db = search_db.open_database('lib')
    assert len(_names(db, 'dog')) == 47 and len(_names(db, 'cat')) == 48
    assert db.row_count() == 95                                                              # trashed + still-processing excluded

    sort_rows = _blob_rows(svc, storage_utils._sort_index_json_blob_name('lib'))
    access_rows = _blob_rows(svc, storage_utils._access_index_json_blob_name('lib'))
    assert len(sort_rows) == 96                                                              # everything not trashed (incl. pending)
    assert len(access_rows) == 97 and any(r['RowKey'] == 'IMG_trashed.jpg' for r in access_rows)

    lexical = json.loads(svc.store['lexical-index/' + storage_utils._lexical_index_manifest_blob_name('lib')])
    assert search_db.is_current('lib', lexical['sourceVersion'])
    assert storage_utils.get_user_index_build_state('lib')['indexes']['lexical'] is True
    assert not [p for p in __import__('os').listdir(index_files.WORK_DIR) if p.startswith('bootstrap-')]   # spool cleaned up


def test_a_restart_resumes_from_the_cursor_and_ends_identical(world, monkeypatch):
    svc, table = world
    real_publish = search_db.publish_delta
    calls = {'n': 0}

    def dies_on_third_chunk(user_id, upserts, deletes, **kw):
        calls['n'] += 1
        if calls['n'] == 3:
            raise RuntimeError('worker killed')
        return real_publish(user_id, upserts, deletes, **kw)

    monkeypatch.setattr(search_db, 'publish_delta', dies_on_third_chunk)
    with pytest.raises(RuntimeError):
        library_build.bootstrap_library_build('lib')
    building = _manifest(svc)['building']
    assert building['chunks'] == 2 and building['rows'] == 40 and building['cursor'] == 'IMG_0039.jpg'
    db = search_db.open_database('lib')
    assert db.row_count() == 40                                                              # partial results are already searchable

    monkeypatch.setattr(search_db, 'publish_delta', real_publish)
    scans = len(table.calls)
    result = library_build.bootstrap_library_build('lib')
    assert result['status'] == 'built'
    assert "RowKey gt 'IMG_0039.jpg'" in table.calls[scans]                                   # did not rescan what was done
    assert search_db.open_database('lib').row_count() == 95
    assert len(_blob_rows(svc, storage_utils._sort_index_json_blob_name('lib'))) == 96
    assert len(_blob_rows(svc, storage_utils._access_index_json_blob_name('lib'))) == 97


def test_a_finished_library_is_left_alone(world):
    svc, table = world
    library_build.bootstrap_library_build('lib')
    scans = len(table.calls)
    assert library_build.bootstrap_library_build('lib')['status'] == 'exists'
    assert len(table.calls) == scans and library_build.bootstrap_needed('lib') is False


def test_a_lost_spool_falls_back_to_scanning_for_the_sort_and_access_indexes(world, monkeypatch):
    svc, table = world
    real = library_build._assemble_rows_index
    monkeypatch.setattr(library_build, '_assemble_rows_index', lambda *a, **k: False)       # ephemeral disk was wiped
    result = library_build.bootstrap_library_build('lib')
    assert result['status'] == 'built' and result.get('sortFallback') and result.get('accessFallback')
    assert len(_blob_rows(svc, storage_utils._sort_index_json_blob_name('lib'))) == 96


def test_an_old_schema_database_is_rebuilt_from_scratch(world, monkeypatch):
    svc, table = world
    library_build.bootstrap_library_build('lib')
    key = 'lexical-index/' + search_db._blob_names('lib')[1]
    old = _manifest(svc)
    old['schemaVersion'] = 'sqlite-v1'
    svc.store[key] = json.dumps(old).encode()
    assert library_build.bootstrap_needed('lib') is True
    assert library_build.bootstrap_library_build('lib')['status'] == 'built'
    assert _manifest(svc)['schemaVersion'] == search_db.SCHEMA_VERSION


def test_progress_is_reported_per_chunk(world):
    seen = []
    library_build.bootstrap_library_build('lib', on_progress=seen.append)
    assert [p['chunks'] for p in seen] == [1, 2, 3, 4, 5] and seen[-1]['rows'] == 97


def test_summaries_come_from_the_finished_database(world, monkeypatch):
    got = []
    monkeypatch.setattr(library_build, 'FINALIZE_HOOK', lambda uid, db: got.append(db.row_count()))
    library_build.bootstrap_library_build('lib')
    assert got == [95]


def test_a_library_past_the_client_limit_skips_the_sort_index_entirely(world, monkeypatch):
    svc, table = world
    monkeypatch.setattr(storage_utils, 'SORT_INDEX_MAX_ROWS', 50)               # 97 photos > 50
    result = library_build.bootstrap_library_build('lib')
    assert result['status'] == 'built' and result.get('sortSkipped')
    manifest = json.loads(svc.store['lexical-index/' + storage_utils._sort_index_manifest_blob_name('lib')])
    assert manifest['skipped'] is True and manifest['rowCount'] == 97
    assert 'lexical-index/' + storage_utils._sort_index_json_blob_name('lib') not in svc.store
    assert storage_utils.get_user_index_build_state('lib')['indexes']['sort'] is True      # still counts as ready

    # later maintenance neither rebuilds it nor marks anything dirty for it
    scans = len(table.calls)
    storage_utils.refresh_user_sort_index('lib')
    assert len(table.calls) == scans
    storage_utils.touch_user_search_indexes_state('lib', filenames=['IMG_0001.jpg'])
    dirty = storage_utils._CTX['search_index_dirty_table_client']
    storage_utils._flush_dirty_filename_buffer('lib', 'lexical')
    assert not any('sort' in row['PartitionKey'] for row in dirty.rows.values())


def test_a_rating_edit_reaches_the_search_database_as_a_delta(world):
    svc, table = world
    library_build.bootstrap_library_build('lib')
    row = next(r for r in table.rows if r['RowKey'] == 'IMG_0004.jpg')
    row['rating'] = 5
    storage_utils.touch_user_sort_index_dirty('lib', ['IMG_0004.jpg'])         # what the rating route calls
    outcome = storage_utils.refresh_user_search_db_incremental('lib')
    assert outcome['status'] == 'delta'
    db = search_db.open_database('lib')
    assert 'IMG_0004.jpg' in db.filter_page(min_rating=5)[0]
