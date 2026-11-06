"""Maintenance reductions retain source fencing and repair semantics."""
from unittest.mock import Mock

import pytest

import app
import storage_utils as su
from tests.test_metadata_write_concurrency import _RacyTable


@pytest.fixture
def metadata(monkeypatch):
    table = _RacyTable()
    table.upsert_entity(dict(PartitionKey='u', RowKey='a.jpg', faceCount=0,
                             processing_metadata='{}', processing_complete=False))
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', False)
    monkeypatch.setattr(su, 'publish_metadata_index_intent', lambda *a: None)
    monkeypatch.setattr(app, '_photo_processing_complete', lambda row: False)
    monkeypatch.setattr(app, 'touch_user_search_indexes_state', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'touch_user_sort_index_dirty', lambda *a: None)
    return table


def test_captured_etag_avoids_duplicate_metadata_read(metadata):
    captured = metadata.get_entity('u', 'a.jpg')
    before = metadata.get_calls
    assert app._update_metadata_entity_fields('u', 'a.jpg', {'faceCount': 1}, initial_entity=captured)
    assert metadata.get_calls == before
    assert metadata.rows['u', 'a.jpg']['faceCount'] == 1
    assert captured['faceCount'] == 0


def test_stale_capture_retries_without_overwriting_newer_fields(metadata, monkeypatch):
    monkeypatch.setattr(app.time, 'sleep', lambda _: None)
    captured = metadata.get_entity('u', 'a.jpg')
    metadata.upsert_entity({**captured, 'caption': 'concurrent edit'})
    before = metadata.get_calls
    assert app._update_metadata_entity_fields('u', 'a.jpg', {'faceCount': 1}, initial_entity=captured)
    assert metadata.get_calls == before + 1
    assert metadata.rows['u', 'a.jpg']['caption'] == 'concurrent edit'


def test_stale_capture_cannot_resurrect_a_deleted_photo(metadata, monkeypatch):
    monkeypatch.setattr(app.time, 'sleep', lambda _: None)
    captured = metadata.get_entity('u', 'a.jpg')
    metadata.upsert_entity({**captured, 'processing_state': 'deleted'})
    assert app._update_metadata_entity_fields('u', 'a.jpg', {'faceCount': 1}, initial_entity=captured) is None
    assert metadata.rows['u', 'a.jpg']['processing_state'] == 'deleted'
    assert metadata.rows['u', 'a.jpg']['faceCount'] == 0


def test_unversioned_capture_uses_fresh_read(metadata):
    captured = dict(metadata.get_entity('u', 'a.jpg'))
    before = metadata.get_calls
    assert app._update_metadata_entity_fields('u', 'a.jpg', {'faceCount': 1}, initial_entity=captured)
    assert metadata.get_calls == before + 1


def test_shared_photo_memberships_are_rebuilt_once(monkeypatch):
    people = Mock()
    people.get_entity.side_effect = [dict(labelMembershipPending=True, faceIds='["a"]'),
                                     dict(labelMembershipPending=True, faceIds='["b"]')]
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, '_load_face_rows_by_ids', lambda user, ids: {ids[0]: {'filename': 'shared.jpg'}})
    rebuild = Mock()
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', rebuild)
    app._update_label_memberships('u', ['p1', 'p2'])
    rebuild.assert_called_once_with('u', {'shared.jpg'}, strict=True)


def test_worker_sqlite_cache_uses_smaller_budget(tmp_path, monkeypatch):
    import sqlite3
    import search_db
    path = tmp_path / 'db.sqlite'
    sqlite3.connect(path).close()
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.delenv('SEARCH_DB_SQLITE_CACHE_KB', raising=False)
    db = search_db.SearchDatabase(str(path))
    assert db._conn().execute('PRAGMA cache_size').fetchone()[0] == -4096
    db._conn().close()


def test_unchanged_summaries_do_not_upload_but_partial_outputs_repair():
    import summary_runtime
    explore = dict(sourceVersion='snapshot:1', places=[], updatedAt='new')
    timeline = dict(sourceVersion='snapshot:1', days=[], updatedAt='new')
    saved = {'explore': {**explore, 'updatedAt': 'old'}, 'timeline': {**timeline, 'days': ['corrupt']}}
    writes = []

    def store(kind, value):
        writes.append(kind)
        saved[kind] = value

    summary_runtime.publish_indexed_payloads('u', explore, timeline,
        store_explore=lambda u, v: store('explore', v), store_timeline=lambda u, v: store('timeline', v),
        load_explore=lambda u: saved['explore'], load_timeline=lambda u: saved['timeline'], skip_unchanged=True)
    assert writes == ['timeline']
    assert saved['explore']['updatedAt'] == 'old'
    assert saved['timeline'] == timeline


def test_changed_summary_upload_failure_still_requires_retry():
    import summary_runtime
    with pytest.raises(RuntimeError, match='Explore summary publication'):
        summary_runtime.publish_indexed_payloads('u', {'sourceVersion': 'new'}, {'sourceVersion': 'new'},
            store_explore=lambda *a: None, store_timeline=lambda *a: None,
            load_explore=lambda u: {'sourceVersion': 'old'}, load_timeline=lambda u: None, skip_unchanged=True)


def test_worker_backs_off_empty_polls_but_keeps_recovery_separate_from_gc(monkeypatch):
    from types import SimpleNamespace
    import index_ownership
    times = [0, 61, 122, 183, 901]
    clock = SimpleNamespace(now=0)
    waits, sweeps = [], []

    class Stop:
        cycle = 0

        def is_set(self):
            return self.cycle == len(times)

        def wait(self, seconds):
            waits.append(seconds)
            self.cycle += 1
            clock.now = times[min(self.cycle, len(times) - 1)]

        def set(self):
            self.cycle = len(times)

    stop = Stop()
    queue = SimpleNamespace(create_queue=lambda: None)
    monkeypatch.setattr(app.threading, 'Event', lambda: stop)
    monkeypatch.setattr(app.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(app.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(index_ownership, 'require_catalog_owner', lambda: None)
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'maintenance')
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda name: queue))
    monkeypatch.setattr(app, 'index_maintenance_queue_client', queue)
    monkeypatch.setattr(app, 'index_maintenance_deferred_queue_client', queue)
    monkeypatch.setattr(app, '_poll_clustering_queue_once', lambda *a, **kw: False)
    monkeypatch.setattr(app, 'recover_pending_index_work', lambda **kw: sweeps.append(kw['cleanup']))
    monkeypatch.setenv('INDEX_WORKER_IDLE_POLL_MAX_SECONDS', '8')
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_INTERVAL_SECONDS', '900')
    app.run_index_worker.__wrapped__()
    assert waits == [2, 4, 8, 8, 8]
    assert sweeps == [True, False, False, False, True]


def test_large_label_repair_uses_bounded_pages_and_deduplicates_across_people(monkeypatch):
    people = Mock()
    people.get_entity.return_value = dict(labelMembershipPending=True, faceIds='["all"]')
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, '_load_face_rows_by_ids', lambda user, ids:
        {str(i): {'filename': f'{i:05d}.jpg'} for i in range(1000)})
    # Include all IDs so the fallback reader is unnecessary.
    people.get_entity.return_value['faceIds'] = __import__('json').dumps([str(i) for i in range(1000)])
    pages = []
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda user, names, **kw: pages.append(names))
    app._update_label_memberships('u', ['one', 'two'])
    assert max(map(len, pages)) <= 128
    assert sum(map(len, pages)) == 1000
    assert len(set.union(*pages)) == 1000
