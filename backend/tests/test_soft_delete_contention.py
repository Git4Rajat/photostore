"""Bulk soft delete must not fail the whole request when index-coverage writes contend.

A 40-photo delete used to run 16 per-row writers at once, each opening its own coverage reservation on one hot
control row; they lost the race and the request returned 500. Rows now go in one transaction per 100, and the
per-row fallback is narrow and isolates a failing row.
"""
import threading
import time

import pytest

import app
from tests.fakes import FakeTable


def _setup(monkeypatch, count):
    meta, trash = FakeTable(), FakeTable()
    names = [f'img{i:04d}.jpg' for i in range(count)]
    for name in names:
        meta.upsert_entity({'PartitionKey': 'u1', 'RowKey': name, 'processing_state': 'active'})
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(app, 'trash_index_table_client', trash)
    monkeypatch.setattr(app, 'BROWSER_PROCESSING_STATUS_FIELDS', [], raising=False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: (lambda r: dict(r) if r else None)(meta.rows.get((uid, name))))
    monkeypatch.setattr(app, '_set_faces_source_deleted_for_filenames', lambda *a, **k: {})
    monkeypatch.setattr(app, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda *a, **k: None)
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda *a, **k: None)
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda *a, **k: None)
    return meta, trash, names


def test_rows_are_deleted_in_one_transaction_per_hundred(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 250)
    result = app._soft_delete_photos_now('u1', names)
    assert result['deleted'] == names and result['errors'] == [] and result['success']
    assert [len(call) for call in meta.submit_transaction_calls] == [100, 100, 50]
    assert all(meta.rows[('u1', n)]['processing_state'] == 'deleted' for n in names)
    assert {key[1] for key in trash.rows} == set(names)


def test_a_lost_race_is_retried_as_a_group_and_still_deletes_everything(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 40)
    real = meta.submit_transaction

    def racing(operations):
        meta.upsert_entity({**meta.rows[('u1', names[3])], 'rating': 5})  # another writer bumps one row's ETag
        meta.submit_transaction = real
        return real(operations)

    meta.submit_transaction = racing
    result = app._soft_delete_photos_now('u1', names)
    assert sorted(result['deleted']) == names and result['errors'] == []
    assert all(meta.rows[('u1', n)]['processing_state'] == 'deleted' for n in names)
    assert meta.rows[('u1', names[3])]['rating'] == 5  # the concurrent edit survived
    assert len(meta.submit_transaction_calls) == 2  # the lost race was settled by re-reading and retrying the group


def test_one_failing_row_is_reported_without_failing_the_request(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 40)
    real_update = meta.update_entity

    def update(entity, *args, **kwargs):
        if entity['RowKey'] == names[7]:
            raise RuntimeError('control row lost 20 races')
        return real_update(entity, *args, **kwargs)

    meta.update_entity = update
    meta.submit_transaction = lambda operations: (_ for _ in ()).throw(RuntimeError('batch lost the race'))
    result = app._soft_delete_photos_now('u1', names)
    assert names[7] not in result['deleted'] and len(result['deleted']) == 39
    assert result['errors'] == [f'{names[7]}: metadata: soft-delete failed']
    assert result['success']
    assert meta.rows[('u1', names[7])]['processing_state'] == 'active'


def test_fallback_writes_stay_within_the_safe_concurrency(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 40)
    live, peak, lock = 0, 0, threading.Lock()
    real_update = meta.update_entity

    def update(entity, *args, **kwargs):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.005)
        try:
            return real_update(entity, *args, **kwargs)
        finally:
            with lock:
                live -= 1

    meta.update_entity = update
    meta.submit_transaction = lambda operations: (_ for _ in ()).throw(RuntimeError('batch lost the race'))
    result = app._soft_delete_photos_now('u1', names)
    assert len(result['deleted']) == 40
    assert peak <= app.DELETE_SOURCE_WRITE_CONCURRENCY


def test_trashed_row_with_a_failed_trash_index_write_is_reported_for_retry(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 5)
    real_upsert = trash.upsert_entity

    def upsert(entity, *args, **kwargs):
        if entity['RowKey'] == names[2]:
            raise RuntimeError('trash index unavailable')
        return real_upsert(entity, *args, **kwargs)

    trash.upsert_entity = upsert
    result = app._soft_delete_photos_now('u1', names)
    assert names[2] not in result['deleted'] and len(result['deleted']) == 4
    assert 'trash index' in result['errors'][0]
    assert meta.rows[('u1', names[2])]['processing_state'] == 'deleted'
    trash.upsert_entity = real_upsert
    retry = app._soft_delete_photos_now('u1', [names[2]])  # replay repairs the trash-index entry
    assert retry['deleted'] == [names[2]] and ('u1', names[2]) in trash.rows


def test_missing_rows_are_not_found_not_errors_of_the_batch(monkeypatch):
    meta, trash, names = _setup(monkeypatch, 3)
    result = app._soft_delete_photos_now('u1', names + ['ghost.jpg'])
    assert result['deleted'] == names and result['errors'] == ['ghost.jpg: Not found']
