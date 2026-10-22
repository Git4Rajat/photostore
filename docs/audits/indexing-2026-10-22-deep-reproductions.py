"""Diagnostic reproductions: passing tests assert observed defects, not correctness."""
import gzip
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

import app
import index_publication
import search_db
import storage_utils as su
from index_work import WorkStore
from test_index_work import EtagTable
from tests.fakes import FakeTable


class BlobStore:
    def __init__(self):
        self.data, self.versions = {}, {}

    def client(self, name):
        store = self

        class Blob:
            def get_blob_properties(self):
                if name not in store.data:
                    raise ResourceNotFoundError('missing')
                return SimpleNamespace(etag=str(store.versions[name]))

            def download_blob(self):
                if name not in store.data:
                    raise ResourceNotFoundError('missing')
                return SimpleNamespace(readall=lambda: store.data[name])

            def upload_blob(self, data, **kw):
                if kw.get('etag') and kw['etag'] != str(store.versions.get(name)):
                    raise ResourceModifiedError('changed')
                if kw.get('overwrite') is False and name in store.data:
                    raise ResourceModifiedError('already exists')
                store.data[name] = data.read() if hasattr(data, 'read') else data
                store.versions[name] = store.versions.get(name, 0) + 1
                return {'etag': str(store.versions[name])}

        return index_publication.fenced(Blob())


def test_losing_delta_writer_overwrites_the_winning_committed_delta(monkeypatch, tmp_path):
    blobs = BlobStore()
    _, manifest_name = search_db._blob_names('A')
    baseline = {'sourceVersion':'v1', 'lineage':'v1', 'schemaVersion':search_db.SCHEMA_VERSION,
                'deltaSeq':0, 'deltaRows':0, 'rowCount':2}
    blobs.client(manifest_name).upload_blob(json.dumps(baseline).encode())
    monkeypatch.setattr(search_db, '_blob_client', blobs.client)
    store = WorkStore(EtagTable())
    claim_a = store.claim(store.request('A', 'light'))
    claim_b = store.claim(store.request('A', 'people'))
    nested = [False]
    winner = []

    def load(user):
        captured = json.loads(blobs.data[manifest_name])
        if not nested[0]:
            nested[0] = True
            # B captured sequence 0, then A publishes/commits sequence 1.
            with index_publication.publication(store, claim_a):
                winner.append(search_db.publish_delta('A', [search_db.row_record({'RowKey':'a.jpg'})], []))
        return captured

    monkeypatch.setattr(search_db, 'load_manifest', load)
    with index_publication.publication(store, claim_b):
        loser = search_db.publish_delta('A', [search_db.row_record({'RowKey':'b.jpg'})], [])
    assert winner == [1] and loser is None
    assert json.loads(blobs.data[manifest_name])['deltaSeq'] == 1
    committed = json.loads(gzip.decompress(blobs.data[search_db._delta_blob_name('A',1)]))
    assert [row['filename'] for row in committed['upserts']] == ['b.jpg']
    path = str(tmp_path/'reader.sqlite')
    search_db.build_database([],path)
    reader=search_db.SearchDatabase(path)
    reader.apply_delta(committed)
    assert reader.all_filenames()=={'b.jpg'}
    # a.jpg was acknowledged by the winner but is absent from committed data.


def test_index_cleanup_deletes_a_person_named_after_its_snapshot(monkeypatch):
    people, faces = FakeTable(), FakeTable()
    people.upsert_entity({'PartitionKey':'A','RowKey':'p1','name':'','faceIds':'[]'})
    old = dict(people.get_entity('A','p1'))
    people.upsert_entity({**old, 'name':'Alice', 'labelPending':'new-intent'})
    result = su._people_index_rows_from_scan(
        'A','v2',[old],SimpleNamespace(get_many=lambda ids:{}),people,faces)
    assert result.rows == []
    with pytest.raises(ResourceNotFoundError):
        people.get_entity('A','p1')


def test_dirty_people_manifest_forces_full_scan_despite_one_affected_id(monkeypatch):
    previous = su.LexicalIndexSnapshot(user_id='A',source_version='published',
        schema_version=su._PEOPLE_INDEX_SCHEMA_VERSION,updated_at='published',rows=[])
    monkeypatch.setattr(su,'_load_people_index_manifest',lambda uid:{
        'sourceVersion':'edit-version','dirty':True,'schemaVersion':su._PEOPLE_INDEX_SCHEMA_VERSION})
    monkeypatch.setattr(su,'_load_people_index_blob',lambda uid:previous)
    incremental = Mock(side_effect=AssertionError('should be reachable for dirty IDs'))
    full = Mock(return_value=None)
    monkeypatch.setattr(su,'_incremental_people_index_snapshot_by_ids',incremental)
    monkeypatch.setattr(su,'_build_user_people_index_snapshot',full)
    assert su.refresh_user_people_index('A',affected_person_ids={'p1'}) is None
    incremental.assert_not_called()
    full.assert_called_once()


def test_missing_filename_lookup_permanently_blocks_first_name_sync(monkeypatch):
    people, faces, metadata = FakeTable(), FakeTable(), FakeTable()
    people.upsert_entity({'PartitionKey':'A','RowKey':'p1','name':'Alice',
        'faceIds':'["f1"]','labelPending':'r1','labelMembershipPending':True})
    faces.upsert_entity({'PartitionKey':'A','RowKey':'f1','filename':'one.jpg','personId':'p1'})
    metadata.upsert_entity({'PartitionKey':'A','RowKey':'one.jpg','peopleIds':'[]','faces':'[]'})
    monkeypatch.setattr(app,'person_table_client',people)
    monkeypatch.setattr(app,'face_table_client',faces)
    monkeypatch.setattr(app,'metadata_table_client',metadata)
    monkeypatch.setattr(app,'_load_searchable_person_name_index',lambda uid:{'p1':'Alice'})
    monkeypatch.setattr(app,'get_face_ids_for_filename',lambda *args:None)
    monkeypatch.setattr(app,'_load_face_rows_by_ids',lambda *args:{'f1':dict(faces.get_entity('A','f1'))})
    for _ in range(2):
        with pytest.raises(RuntimeError,match='Face filename lookup unavailable'):
            app._update_label_memberships('A', {'p1'})
    assert people.get_entity('A','p1')['labelMembershipPending']
    assert metadata.get_entity('A','one.jpg')['peopleIds']=='[]'


def test_recovery_send_failure_prevents_later_library_notifications():
    store = WorkStore(EtagTable(),clock=lambda:1000.0)
    store.request('broken','people')
    store.request('healthy','search')
    sent=[]
    def send(library,*args):
        if library=='broken':
            raise OSError('queue publication failed for this wakeup')
        sent.append(library)
    with pytest.raises(OSError):
        store.recover(send)
    assert sent == []


def test_source_outbox_is_not_relayed_when_search_base_is_missing(monkeypatch):
    metadata=FakeTable()
    metadata.upsert_entity({'PartitionKey':'A','RowKey':'one.jpg',
        'indexPending':'{"revision":"r1","kinds":["sort","lexical"]}'})
    monkeypatch.setitem(su._CTX,'metadata_table_client',metadata)
    monkeypatch.setattr(search_db,'open_database',lambda uid:None)
    relay=Mock()
    monkeypatch.setattr(su,'publish_metadata_index_intent',relay)
    assert su.reconcile_search_db('A',force=True)=={'status':'no_db'}
    relay.assert_not_called()


def test_dedicated_compaction_and_catchup_never_refresh_explore_summary(monkeypatch):
    monkeypatch.setattr(app,'INDEX_MAINTENANCE_QUEUE_NAME','index-work')
    monkeypatch.setattr(app,'_upsert_job_status',Mock())
    monkeypatch.setattr(search_db,'compact_database',lambda uid:{'sourceVersion':'compacted'})
    enqueue=Mock(return_value='queued')
    monkeypatch.setattr(app,'enqueue_index_build',enqueue)
    monkeypatch.setattr(app,'prime_all_user_indexes_sequentially',lambda *a,**kw:[])
    monkeypatch.setattr(app,'refresh_user_search_db_incremental',lambda uid:{'status':'delta'})
    summaries=Mock()
    monkeypatch.setattr(app,'_refresh_library_summaries',summaries)
    app._run_index_build_job('A','compact-job','compact')
    enqueue.assert_called_once_with('A',reason='post-compact',scope='search',coalesce=False)
    app._run_index_build_job('A','search-job','search')
    summaries.assert_not_called()
