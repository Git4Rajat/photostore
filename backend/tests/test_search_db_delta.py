"""Incremental search database: deltas published by the worker and applied in place on replicas."""
from __future__ import annotations

import json
import threading
from datetime import date

import pytest

import search_db
import search_utils
import storage_utils
from tests.fakes import FakeTable


class _Props:
    def __init__(self, etag):
        self.etag = etag


class _Down:
    def __init__(self, data):
        self._d = data

    def readall(self):
        return self._d

    def readinto(self, fh):
        fh.write(self._d)


class _Blob:
    def __init__(self, svc, key):
        self.svc, self.key = svc, key

    def upload_blob(self, data, overwrite=True, content_settings=None, etag=None, match_condition=None):
        if etag is not None and self.svc.etags.get(self.key) != etag:
            raise RuntimeError('ConditionNotMet')
        self.svc.store[self.key] = data.read() if hasattr(data, 'read') else bytes(data)
        self.svc.etags[self.key] = f'e{len(self.svc.etags)}-{len(self.svc.store[self.key])}-{self.svc.writes}'
        self.svc.writes += 1

    def download_blob(self):
        if self.key not in self.svc.store:
            raise type('ResourceNotFoundError', (Exception,), {})('missing')
        return _Down(self.svc.store[self.key])

    def get_blob_properties(self):
        return _Props(self.svc.etags.get(self.key))

    def delete_blob(self):
        self.svc.store.pop(self.key, None)


class _Svc:
    def __init__(self):
        self.store, self.etags, self.writes = {}, {}, 0

    def get_blob_client(self, container, blob):
        return _Blob(self, f'{container}/{blob}')


def _row(name, tags, day='2021-03-04', rating=0, **extra):
    return {'PartitionKey': 'lib', 'RowKey': name, 'processing_complete': True, 'tags': json.dumps(tags),
            'subjectTags': json.dumps(tags[:1]), 'uploadDate': f'{day}T10:00:00+00:00', 'rating': rating, **extra}


@pytest.fixture
def world(monkeypatch, tmp_path):
    svc, meta, dirty = _Svc(), FakeTable(), FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', svc)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', meta)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setattr(search_db, 'SEARCH_DB_DIR', str(tmp_path / 'ephemeral'))
    monkeypatch.setattr(search_db, 'DELTA_MAX_ROW_FRACTION', 100.0)   # tiny test base: never compact unless asked
    search_db._OPEN.clear()
    base = [_row('a.jpg', ['dog', 'grass']), _row('b.jpg', ['beach', 'sunset']), _row('c.jpg', ['dogs'], day='2022-01-01')]
    for r in base:
        meta.upsert_entity(r)

    class _Snap:
        source_version = 'v1'
        updated_at = 'v1'
        rows = base
    assert search_db.write_for_snapshot('lib', _Snap) is True
    return svc, meta, dirty


def _names(db, query):
    return sorted(f for f, _ in db.candidates(search_db.match_terms(search_utils.parse_search_query(query))))


def _change(meta, row):
    meta.upsert_entity(row)
    storage_utils.touch_user_search_indexes_state('lib', filenames=row['RowKey'])


def test_an_update_an_insert_and_a_removal_become_searchable_without_a_rebuild(world):
    svc, meta, dirty = world
    db = search_db.open_database('lib')
    assert _names(db, 'dog') == ['a.jpg', 'c.jpg'] and db.applied_seq() == 0

    _change(meta, _row('a.jpg', ['cat'], rating=5))                           # retagged
    _change(meta, _row('d.jpg', ['dog', 'puppy'], day='2023-05-06'))          # brand new upload
    _change(meta, {**_row('b.jpg', ['beach']), 'processing_state': 'deleted'})  # trashed

    outcome = storage_utils.refresh_user_search_db_incremental('lib')
    assert outcome['status'] == 'delta' and outcome['upserts'] == 2 and outcome['deletes'] == 1

    db = search_db.open_database('lib')                                       # replica syncs the delta in place
    assert db.applied_seq() == 1
    assert _names(db, 'dog') == ['c.jpg', 'd.jpg']                            # a.jpg lost 'dog' (FTS entry really removed)
    assert _names(db, 'cat') == ['a.jpg'] and _names(db, 'puppy') == ['d.jpg']
    assert _names(db, 'beach') == []                                          # trashed photo is gone
    names, total = db.list_page(sort='capture', offset=0, limit=10)
    assert total == 3 and set(names) == {'a.jpg', 'c.jpg', 'd.jpg'}
    assert db.filter_page(min_rating=5)[0] == ['a.jpg']
    assert db.timeline_summary(today=date(2026, 1, 1))['totalCount'] == 3
    # the dirty marks were consumed, so a second pass has nothing to do
    assert storage_utils.refresh_user_search_db_incremental('lib')['status'] == 'noop'


def test_a_replica_that_joins_later_applies_every_delta_in_order(world):
    svc, meta, dirty = world
    for i in range(3):
        _change(meta, _row(f'n{i}.jpg', ['bird']))
        assert storage_utils.refresh_user_search_db_incremental('lib')['status'] == 'delta'
    assert json.loads(svc.store['lexical-index/' + search_db._blob_names('lib')[1]])['deltaSeq'] == 3
    search_db._OPEN.clear()
    import shutil
    shutil.rmtree(search_db.SEARCH_DB_DIR, ignore_errors=True)                # fresh replica: base only
    db = search_db.open_database('lib')
    assert db.applied_seq() == 3 and _names(db, 'bird') == ['n0.jpg', 'n1.jpg', 'n2.jpg']


def test_readers_keep_working_while_a_delta_is_applied(world):
    svc, meta, dirty = world
    db = search_db.open_database('lib')
    errors, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                db.list_page(sort='capture', offset=0, limit=5)
                _names(db, 'dog')
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for i in range(5):
        _change(meta, _row(f'w{i}.jpg', ['dog']))
        storage_utils.refresh_user_search_db_incremental('lib')
        search_db.open_database('lib')
    stop.set()
    for t in threads:
        t.join()
    assert errors == [] and db.applied_seq() == 5


def test_too_much_change_asks_for_a_compacting_rebuild(world, monkeypatch):
    svc, meta, dirty = world
    monkeypatch.setattr(search_db, 'DELTA_MAX_ROW_FRACTION', 0.5)             # 3 base rows -> at most 1 changed row
    _change(meta, _row('x.jpg', ['dog']))
    _change(meta, _row('y.jpg', ['dog']))
    assert storage_utils.refresh_user_search_db_incremental('lib')['status'] == 'needs_full'
    # nothing was lost: the names are still dirty for the rebuild
    assert storage_utils._get_dirty_search_index_filenames('lib', 'lexical') >= {'x.jpg', 'y.jpg'}


def test_a_full_rebuild_replaces_the_base_and_discards_old_deltas(world):
    svc, meta, dirty = world
    _change(meta, _row('e.jpg', ['dog']))
    storage_utils.refresh_user_search_db_incremental('lib')
    delta_key = 'lexical-index/' + search_db._delta_blob_name('lib', 1)
    assert delta_key in svc.store

    class _Snap2:
        source_version = 'v2'
        updated_at = 'v2'
        rows = [_row('only.jpg', ['fox'])]
    assert search_db.write_for_snapshot('lib', _Snap2) is True
    assert delta_key not in svc.store
    manifest = json.loads(svc.store['lexical-index/' + search_db._blob_names('lib')[1]])
    assert manifest['sourceVersion'] == 'v2' and manifest['deltaSeq'] == 0
    db = search_db.open_database('lib')
    assert _names(db, 'fox') == ['only.jpg'] and _names(db, 'dog') == []


def test_a_delta_never_overwrites_a_manifest_that_moved_on(world, monkeypatch):
    svc, meta, dirty = world
    _change(meta, _row('z.jpg', ['dog']))
    manifest_key = 'lexical-index/' + search_db._blob_names('lib')[1]
    original = _Blob.upload_blob

    def upload_then_rebuild_lands(self, data, **kw):
        original(self, data, **kw)
        if self.key.endswith('-searchdelta-000001.json.gz'):
            moved = json.loads(svc.store[manifest_key])
            moved['sourceVersion'] = moved['lineage'] = 'v-rebuilt'   # a full rebuild published a new base lineage
            svc.store[manifest_key] = json.dumps(moved).encode()
            svc.etags[manifest_key] = 'changed-by-rebuild'

    monkeypatch.setattr(_Blob, 'upload_blob', upload_then_rebuild_lands)
    outcome = storage_utils.refresh_user_search_db_incremental('lib')
    assert outcome['status'] == 'conflict'
    assert json.loads(svc.store[manifest_key])['sourceVersion'] == 'v-rebuilt'      # the rebuild's manifest survived
    assert json.loads(svc.store[manifest_key])['deltaSeq'] == 0                      # and our delta was not advertised
    assert 'z.jpg' in storage_utils._get_dirty_search_index_filenames('lib', 'lexical')  # still dirty for the next pass


def test_no_base_means_the_incremental_path_does_nothing(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', _Svc())
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    assert storage_utils.refresh_user_search_db_incremental('nobody')['status'] == 'no_base'


def test_dirty_table_outage_is_not_treated_as_nothing_changed(world, monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    assert storage_utils.refresh_user_search_db_incremental('lib')['status'] == 'unavailable'


def test_compaction_folds_the_log_into_a_new_base_without_touching_the_table(world, monkeypatch, tmp_path):
    svc, meta, dirty = world
    monkeypatch.setenv('INDEX_BUILD_SQLITE_DIR', str(tmp_path / 'build'))
    for i in range(3):
        _change(meta, _row(f'k{i}.jpg', ['otter']))
        storage_utils.refresh_user_search_db_incremental('lib')
    # the table must not be read at all while compacting
    meta.query_entities = lambda *a, **k: (_ for _ in ()).throw(AssertionError('compaction must not scan the table'))
    updated = search_db.compact_database('lib')
    assert updated and updated['baseSeq'] == 3 and updated['deltaSeq'] == 3 and updated['lineage'] == 'v1'
    assert not any('searchdelta' in k for k in svc.store)                     # folded deltas are gone
    # a replica joining now downloads ONE file (seq 3 inside it) and has nothing left to replay
    import shutil
    search_db._OPEN.clear()
    shutil.rmtree(search_db.SEARCH_DB_DIR, ignore_errors=True)
    db = search_db.open_database('lib')
    assert db.applied_seq() == 3 and _names(db, 'otter') == ['k0.jpg', 'k1.jpg', 'k2.jpg']
    assert len(_names(db, 'dog')) == 2


def test_deltas_published_after_a_compaction_apply_on_top_of_the_new_base(world, monkeypatch, tmp_path):
    svc, meta, dirty = world
    monkeypatch.setenv('INDEX_BUILD_SQLITE_DIR', str(tmp_path / 'build'))
    _change(meta, _row('p1.jpg', ['heron']))
    storage_utils.refresh_user_search_db_incremental('lib')
    search_db.compact_database('lib')
    _change(meta, _row('p2.jpg', ['heron']))
    outcome = storage_utils.refresh_user_search_db_incremental('lib')
    assert outcome['status'] == 'delta'
    import shutil
    search_db._OPEN.clear()
    shutil.rmtree(search_db.SEARCH_DB_DIR, ignore_errors=True)
    db = search_db.open_database('lib')
    assert db.applied_seq() == 2 and _names(db, 'heron') == ['p1.jpg', 'p2.jpg']


def test_compaction_does_not_clobber_a_manifest_from_another_lineage(world, monkeypatch, tmp_path):
    svc, meta, dirty = world
    monkeypatch.setenv('INDEX_BUILD_SQLITE_DIR', str(tmp_path / 'build'))
    _change(meta, _row('q.jpg', ['stoat']))
    storage_utils.refresh_user_search_db_incremental('lib')
    key = 'lexical-index/' + search_db._blob_names('lib')[1]
    real_upload = search_db._upload_database_file

    def upload_then_rebuild_lands(user_id, path, label):
        real_upload(user_id, path, label)
        moved = json.loads(svc.store[key])
        moved['lineage'] = moved['sourceVersion'] = 'rebuilt'
        svc.store[key] = json.dumps(moved).encode()
        svc.etags[key] = 'x'

    monkeypatch.setattr(search_db, '_upload_database_file', upload_then_rebuild_lands)
    assert search_db.compact_database('lib') is None
    assert json.loads(svc.store[key])['lineage'] == 'rebuilt'


def test_a_photo_still_being_processed_is_listed_right_away(world):
    svc, meta, dirty = world
    _change(meta, {**_row('new.jpg', []), 'processing_complete': False, 'thumbnail_status': 'done'})   # just uploaded
    assert storage_utils.refresh_user_search_db_incremental('lib')['upserts'] == 1
    db = search_db.open_database('lib')
    names, total = db.list_page(sort='capture', offset=0, limit=10)
    assert 'new.jpg' in names and total == 4                    # visible to the gallery/Workbench/covers before OCR/faces finish


def test_marks_reach_the_table_without_a_reader_in_the_same_process(world, monkeypatch):
    """The process that marks a photo (upload, ipworker) is not the one that builds the index (worker): a
    handful of marks must be written out on their own, not only once 100 pile up or a reader in the same
    process asks."""
    svc, meta, dirty = world
    storage_utils._DIRTY_FILENAME_BUFFER.clear()
    storage_utils.touch_user_search_indexes_state('lib', filenames=['n1.jpg', 'n2.jpg'])        # 2 << batch of 100
    assert not [r for r in dirty.rows if r[0].endswith('lexical')]                              # still only in memory
    storage_utils.flush_all_dirty_filename_buffers()
    assert {r[1] for r in dirty.rows if 'lexical' in r[0]} == {'n1.jpg', 'n2.jpg'}


def test_a_timer_flushes_small_batches(world, monkeypatch):
    import time as _time
    svc, meta, dirty = world
    storage_utils._DIRTY_FILENAME_BUFFER.clear()
    monkeypatch.setattr(storage_utils, 'DIRTY_FLUSH_DELAY_SECONDS', 0.05)
    monkeypatch.setattr(storage_utils, '_DIRTY_FLUSH_TIMER', None)
    storage_utils.touch_user_search_indexes_state('lib', filenames=['t1.jpg'])
    deadline = _time.time() + 3
    while _time.time() < deadline and not [r for r in dirty.rows if 'lexical' in r[0]]:
        _time.sleep(0.02)
    assert {r[1] for r in dirty.rows if 'lexical' in r[0]} == {'t1.jpg'}
