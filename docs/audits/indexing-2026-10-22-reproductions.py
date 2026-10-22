import json
from types import SimpleNamespace
from unittest.mock import Mock
import app
import storage_utils as su
import search_db
from tests.fakes import FakeTable


def test_album_maintenance_publishes_old_cover_before_search_catches_up(monkeypatch, tmp_path):
    albums, metadata = FakeTable(), FakeTable()
    albums.upsert_entity({'PartitionKey':'A','RowKey':'album','name':'Trip','filenames':'["a.jpg", "b.jpg"]'})
    pending = json.dumps({'revision':'r1','kinds':sorted(su.metadata_index_kinds({'rating':5}))})
    metadata.upsert_entity({'PartitionKey':'A','RowKey':'b.jpg','rating':5,'indexPending':pending})
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setitem(su._CTX, 'albums_table_client', albums)
    monkeypatch.setitem(su._CTX, 'metadata_table_client', metadata)
    for name in ('touch_user_albums_index_state','touch_user_lexical_index_state','touch_user_sort_index_state', '_mark_search_index_dirty_filenames', '_flush_dirty_filename_buffer'):
        monkeypatch.setattr(su, name, Mock())
    monkeypatch.setattr(su, 'sort_index_skipped', lambda uid:False)
    queued=[]
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda uid,scope:queued.append(scope) or 'queued')
    assert su.publish_metadata_index_intent('A','b.jpg',metadata.get_entity('A','b.jpg'))
    assert queued == ['albums','search','sort']
    path=str(tmp_path/'search.sqlite')
    search_db.build_database([{'RowKey':'a.jpg','rating':4},{'RowKey':'b.jpg','rating':1}],path)
    monkeypatch.setattr(search_db,'open_database',lambda uid:search_db.SearchDatabase(path))
    published=su._build_user_albums_index_snapshot('A','v2')
    assert published.rows[0]['coverFilename']=='a.jpg'
    search_db.build_database([{'RowKey':'a.jpg','rating':4},{'RowKey':'b.jpg','rating':5}],path)
    assert su._build_user_albums_index_snapshot('A','v3').rows[0]['coverFilename']=='b.jpg'
    assert published.rows[0]['coverFilename']=='a.jpg' # No queued albums follow-up after Search.


def test_legacy_edit_during_people_build_remains_pending_without_followup(monkeypatch):
    from routes.people import _save_person_label
    people,dirty=FakeTable(),FakeTable()
    people.upsert_entity({'PartitionKey':'A','RowKey':'p1','name':'Alice','faceIds':'[]'})
    monkeypatch.setattr(app,'person_table_client',people)
    monkeypatch.setattr(app,'people_dirty_set_table_client',dirty)
    monkeypatch.setattr(app,'albums_dirty_set_table_client',None)
    monkeypatch.setattr(app,'INDEX_MAINTENANCE_QUEUE_NAME','')
    monkeypatch.setattr(app,'blob_service_client',None)
    monkeypatch.setattr(app,'_invalidate_people_scan_cache',lambda uid:None)
    monkeypatch.setattr(app,'_upsert_job_status',Mock())
    monkeypatch.setattr(app,'_job_row_fresh_active',lambda *a:True)
    sent=[]
    monkeypatch.setattr(app,'library_ops_queue_client',SimpleNamespace(send_message=lambda *a,**k:sent.append(a)))
    monkeypatch.setattr(app,'_drain_incremental_search_db',lambda *a:{'status':'noop'})
    original_enqueue=app.enqueue_index_build
    first=_save_person_label('A','p1','Bob')
    # First intent was queued before the worker acquired its input snapshot.
    monkeypatch.setattr(app,'enqueue_index_build',Mock(return_value='queued'))
    assert app.publish_person_label_intent('A','p1',first)
    monkeypatch.setattr(app,'enqueue_index_build',original_enqueue)
    projected=[]
    def prime(*args,**kwargs):
        projected.append(people.get_entity('A','p1')['name'])
        concurrent=_save_person_label('A','p1','Carol')
        assert app.publish_person_label_intent('A','p1',concurrent)
        return []
    monkeypatch.setattr(app,'prime_all_user_indexes_sequentially',prime)
    app._run_index_build_job('A','job','people')
    assert projected==['Bob']
    assert people.get_entity('A','p1')['name']=='Carol'
    assert people.get_entity('A','p1')['labelPending']
    assert app._pending_person_labels('A')=={'p1'}
    assert not sent # Source and dirty records survive, but nothing will consume them.

