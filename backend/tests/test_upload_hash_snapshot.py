"""Prebuilt dedup downloads: isolation, bounded reads, deletion safety and jobs."""
import copy
import gzip
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests
from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline.transport import RequestsTransport
from azure.data.tables import TableTransactionError
from azure.storage.blob import BlobClient
from urllib3.response import HTTPResponse

import app
import index_files
import storage_utils as su
import upload_hash_snapshot as snapshots
from tests.test_search_db_delta import _Blob, _Svc
from tests.test_upload_diagnostics import upload_env, file_data


class HashTable:
    def __init__(self):
        self.rows = {}
        self.scans = 0
        self.gets = 0
        self.transactions = []
        self.scan_hook = None

    def get_entity(self, partition_key, row_key):
        self.gets += 1
        try:
            return dict(self.rows[partition_key, row_key])
        except KeyError:
            raise ResourceNotFoundError('missing')

    def upsert_entity(self, entity):
        self.rows[entity['PartitionKey'], entity['RowKey']] = dict(entity)

    def delete_entity(self, partition_key, row_key):
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError('missing')
        del self.rows[partition_key, row_key]

    def query_entities(self, query, **kwargs):
        self.scans += 1
        user_id = query.removeprefix("PartitionKey eq '").removesuffix("'").replace("''", "'")
        rows = [dict(row) for (pk, _), row in self.rows.items() if pk == user_id]
        for row in rows:
            yield row
        if self.scan_hook:
            self.scan_hook()

    def submit_transaction(self, operations):
        self.transactions.append(operations)
        previous = copy.deepcopy(self.rows)
        try:
            for verb, entity in operations:
                if verb == 'upsert':
                    self.upsert_entity(entity)
                else:
                    self.delete_entity(entity['PartitionKey'], entity['RowKey'])
        except Exception:
            self.rows = previous
            raise


class SnapshotBlob(_Blob):
    def download_blob(self, **kwargs):
        return super().download_blob()


class SnapshotBlobs(_Svc):
    def get_blob_client(self, container, blob):
        return SnapshotBlob(self, f'{container}/{blob}')


@pytest.fixture
def world(monkeypatch, tmp_path):
    table, blobs = HashTable(), SnapshotBlobs()
    monkeypatch.setitem(su._CTX, 'hash_index_table_client', table)
    monkeypatch.setitem(su._CTX, 'metadata_table_client', object())
    monkeypatch.setitem(su._CTX, 'blob_service_client', blobs)
    monkeypatch.setitem(su._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setattr(su, 'INDEX_DISK_CACHE_DIR', '')
    monkeypatch.setattr(index_files, 'WORK_DIR', str(tmp_path))
    table.upsert_entity({'PartitionKey': 'lib', 'RowKey': 'a' * 64, 'filename': 'original.heic'})
    table.upsert_entity({'PartitionKey': 'other', 'RowKey': 'b' * 64, 'filename': 'private.heic'})
    return table, blobs


def test_build_streams_one_library_and_reads_without_scanning(world):
    table, blobs = world
    manifest = snapshots.rebuild_snapshot('lib')
    assert manifest['rows'] == 1 and table.scans == 1
    payload, served, refresh = snapshots.read_snapshot('lib')
    assert json.loads(gzip.decompress(payload)) == {'hashes': {'a' * 64: 'original.heic'}}
    assert served == manifest and not refresh and table.scans == 1
    assert snapshots.read_snapshot('other')[0] is None
    assert b'private.heic' not in gzip.decompress(payload)


def test_rebuild_replaces_snapshot_and_removes_superseded_blob(world):
    table, blobs = world
    old = snapshots.rebuild_snapshot('lib')
    table.upsert_entity({'PartitionKey': 'lib', 'RowKey': 'c' * 64, 'filename': 'new.heic'})
    new = snapshots.rebuild_snapshot('lib')
    assert new['rows'] == 2 and new['blobName'] != old['blobName']
    assert 'lexical-index/' + old['blobName'] not in blobs.store
    assert json.loads(gzip.decompress(snapshots.read_snapshot('lib')[0]))['hashes']['c' * 64] == 'new.heic'


def test_delete_invalidates_snapshot_in_same_transaction_as_hash_removal(world):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    su.delete_hash_index_entry('lib', 'a' * 64)
    assert [verb for verb, _ in table.transactions[-1]] == ['upsert', 'delete']
    assert snapshots.read_snapshot('lib') == (None, {}, True)
    assert ('lib', 'a' * 64) not in table.rows
    snapshots.rebuild_snapshot('lib')
    assert json.loads(gzip.decompress(snapshots.read_snapshot('lib')[0])) == {'hashes': {}}


def test_already_missing_hash_still_invalidates_snapshot(world):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    del table.rows['lib', 'a' * 64]
    snapshots.delete_hash(table, 'lib', 'a' * 64)
    assert snapshots.read_snapshot('lib')[0] is None


def test_sdk_transaction_not_found_still_invalidates_snapshot(world, monkeypatch):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    error = TableTransactionError(message='missing')
    error.status_code = 404
    monkeypatch.setattr(table, 'submit_transaction', lambda *a: (_ for _ in ()).throw(error))
    snapshots.delete_hash(table, 'lib', 'a' * 64)
    assert snapshots.read_snapshot('lib')[0] is None


def test_snapshot_control_rows_cannot_be_overwritten_or_deleted_as_upload_hashes(world):
    table, _ = world
    original = snapshots.rebuild_snapshot('lib')
    su._store_hash_index('lib', snapshots.SNAPSHOT_ROW, 'bad.heic')
    su.delete_hash_index_entry('lib', snapshots.SNAPSHOT_ROW)
    assert table.rows['lib', snapshots.SNAPSHOT_ROW] == original


def test_deletion_during_build_does_not_publish_old_hashes(world):
    table, blobs = world
    original = snapshots.rebuild_snapshot('lib')
    table.scan_hook = lambda: snapshots.delete_hash(table, 'lib', 'a' * 64)
    with pytest.raises(RuntimeError, match='deleted during snapshot build'):
        snapshots.rebuild_snapshot('lib')
    assert table.rows['lib', snapshots.SNAPSHOT_ROW] == original
    assert snapshots.read_snapshot('lib')[0] is None
    assert len(blobs.store) == 1


def test_deletion_during_download_is_not_served(world, monkeypatch):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    original = su._get_blob_client

    def client(container, name):
        blob = original(container, name)
        download = blob.download_blob

        def interrupted(**kwargs):
            result = download(**kwargs)
            snapshots.delete_hash(table, 'lib', 'a' * 64)
            return result
        blob.download_blob = interrupted
        return blob

    monkeypatch.setattr(su, '_get_blob_client', client)
    assert snapshots.read_snapshot('lib') == (None, {}, True)


def test_unreadable_delete_epoch_fails_closed(world, monkeypatch):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    original = table.get_entity

    def get(partition_key, row_key):
        if row_key == snapshots.DELETE_EPOCH_ROW:
            raise RuntimeError('storage unavailable')
        return original(partition_key, row_key)
    monkeypatch.setattr(table, 'get_entity', get)
    assert snapshots.read_snapshot('lib') == (None, {}, True)


def test_failed_publication_keeps_old_pointer(world, monkeypatch):
    table, _ = world
    original = snapshots.rebuild_snapshot('lib')
    monkeypatch.setattr(su, '_upload_file_to_blob', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('upload failed')))
    with pytest.raises(RuntimeError, match='upload failed'):
        snapshots.rebuild_snapshot('lib')
    assert table.rows['lib', snapshots.SNAPSHOT_ROW] == original
    assert snapshots.read_snapshot('lib')[0] is not None


def test_missing_snapshot_route_queues_dedup_only_without_scan(world, monkeypatch):
    table, _ = world
    queued = []
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: queued.append((a, kw)))
    response = app.app.test_client().get('/upload/known-hashes')
    assert response.status_code == 200 and response.get_json() == {'hashes': {}, 'available': False}
    assert table.scans == 0 and queued[0][1]['scope'] == 'dedup'
    assert response.headers['Cache-Control'] == 'private, no-store'


def test_ready_route_serves_gzip_without_query_or_build(world, monkeypatch):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: pytest.fail('unneeded rebuild'))
    response = app.app.test_client().get('/upload/known-hashes')
    assert response.status_code == 200 and response.headers['Content-Encoding'] == 'gzip'
    assert json.loads(gzip.decompress(response.data)) == {'hashes': {'a' * 64: 'original.heic'}}
    assert table.scans == 1


@pytest.mark.parametrize('cached_share', [False, True])
def test_ready_route_preserves_gzip_through_real_blob_sdk(world, monkeypatch, cached_share):
    _, blobs = world
    manifest = snapshots.rebuild_snapshot('lib')
    compressed = blobs.store['lexical-index/' + manifest['blobName']]
    headers = {
        'Content-Type': 'application/json', 'Content-Encoding': 'gzip',
        'Content-Length': str(len(compressed)),
        'Content-Range': f'bytes 0-{len(compressed) - 1}/{len(compressed)}',
        'ETag': '"snapshot"', 'x-ms-blob-type': 'BlockBlob',
    }
    session = requests.Session()

    def request(*args, **kwargs):
        response = requests.Response()
        response.status_code = 206
        response.headers.update(headers)
        response.raw = HTTPResponse(body=io.BytesIO(compressed), headers=headers,
                                    status=206, preload_content=False)
        return response

    monkeypatch.setattr(session, 'request', request)
    with BlobClient('https://storage.example', 'lexical-index', manifest['blobName'],
                    transport=RequestsTransport(session=session)) as blob:
        # Unlike the in-memory blob fake, the real SDK decodes gzip by default.
        assert blob.download_blob().readall() == gzip.decompress(compressed)
        reader = blob
        if cached_share:
            reader = su._ShareBackedBlob(blob, 'lexical-index', manifest['blobName'])
            # A previous default download could also cache the decoded JSON.
            monkeypatch.setattr(reader, '_cached_bytes', lambda: gzip.decompress(compressed))
        monkeypatch.setattr(su, '_get_blob_client', lambda *args: reader)
        monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
        response = app.app.test_client().get('/upload/known-hashes')
    assert response.status_code == 200
    assert response.headers['Content-Encoding'] == 'gzip'
    assert response.data == compressed
    assert json.loads(gzip.decompress(response.data)) == {'hashes': {'a' * 64: 'original.heic'}}


def test_old_snapshot_is_served_while_additions_refresh_in_background(world, monkeypatch):
    table, _ = world
    snapshots.rebuild_snapshot('lib')
    table.rows['lib', snapshots.SNAPSHOT_ROW]['updatedAt'] = (
        datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    queued = []
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: queued.append(kw['scope']))
    response = app.app.test_client().get('/upload/known-hashes')
    assert json.loads(gzip.decompress(response.data))['hashes']['a' * 64] == 'original.heic'
    assert queued == ['dedup'] and table.scans == 1


def test_route_does_not_bypass_library_authorization(world, monkeypatch):
    table, _ = world
    monkeypatch.setattr(app, '_require_library_context', lambda: (None, None, (app.jsonify({'error': 'forbidden'}), 403)))
    response = app.app.test_client().get('/upload/known-hashes')
    assert response.status_code == 403 and table.gets == table.scans == 0


@pytest.mark.parametrize('reason', ['completed', 'stopped', 'failed', 'interrupted', 'discarded'])
def test_upload_end_refresh_route_queues_only_dedup(world, monkeypatch, reason):
    table, _ = world
    queued = []
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: queued.append((a, kw)) or 'queued')
    response = app.app.test_client().post('/upload/known-hashes/refresh', json={'reason': reason})
    assert response.status_code == 202 and response.json == {'status': 'queued'}
    assert queued == [(('lib',), {'reason': f'upload-{reason}', 'scope': 'dedup', 'coalesce': False})]
    assert table.scans == table.gets == 0


def test_refresh_route_validates_reason_and_authorization(world, monkeypatch):
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: pytest.fail('unauthorized/invalid build'))
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    assert app.app.test_client().post('/upload/known-hashes/refresh', json={'reason': 'full'}).status_code == 400
    monkeypatch.setattr(app, '_require_library_context', lambda: (None, None, (app.jsonify({'error': 'forbidden'}), 403)))
    assert app.app.test_client().post('/upload/known-hashes/refresh', json={'reason': 'completed'}).status_code == 403


@pytest.mark.parametrize('status,code', [('already_active', 202), ('unavailable', 503)])
def test_refresh_route_reports_coalescing_or_retryable_queue_failure(world, monkeypatch, status, code):
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'lib', None))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: status)
    response = app.app.test_client().post('/upload/known-hashes/refresh', json={'reason': 'completed'})
    assert response.status_code == code and response.json['status'] == status


def test_idle_fallback_is_delayed_and_coalesced(monkeypatch):
    from tests.test_index_priming import _dedicated_queue
    queue = _dedicated_queue(monkeypatch)
    monkeypatch.setattr(app, '_job_row_fresh_active', lambda *a: False)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    assert app.enqueue_index_build('lib', scope='dedup-idle', coalesce=False) == 'queued'
    assert queue.sent[0]['jobId'] == 'index-build-lib-dedup-idle'
    assert queue.delays == [int(app.UPLOAD_ACTIVITY_WINDOW_SECONDS)]
    monkeypatch.setattr(app, '_job_row_fresh_active', lambda *a: True)
    assert app.enqueue_index_build('lib', scope='dedup-idle', coalesce=False) == 'already_active'
    assert len(queue.sent) == 1


@pytest.mark.parametrize('batch', [False, True])
def test_successful_finalize_schedules_one_durable_idle_check(upload_env, monkeypatch, batch):
    _, client = upload_env
    queued = []
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: queued.append((a, kw)) or 'queued')
    response = client.post('/upload/finalize-batch' if batch else '/upload/finalize',
                           json={'files': [file_data(), file_data()]} if batch else file_data())
    assert response.status_code == 200
    assert queued == [(('library',), {'reason': 'upload-quiet', 'scope': 'dedup-idle', 'coalesce': False})]


def test_failed_finalize_does_not_schedule_idle_refresh(upload_env, monkeypatch):
    _, client = upload_env
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: pytest.fail('no successful uploads'))
    response = client.post('/upload/finalize-batch', json={'files': [file_data(totalSize=0)]})
    assert response.status_code == 200 and not response.json['results'][0].get('complete')


def test_idle_refresh_queue_failure_does_not_fail_uploaded_files(upload_env, monkeypatch):
    _, client = upload_env

    def fail(*args, **kwargs):
        raise RuntimeError('queue unavailable')

    monkeypatch.setattr(app, 'enqueue_index_build', fail)
    response = client.post('/upload/finalize', json=file_data())
    assert response.status_code == 200 and response.json['complete']


def test_idle_fallback_reschedules_while_upload_is_active_without_scan(world, monkeypatch):
    from tests.test_index_priming import _dedicated_queue
    table, _ = world
    queue, statuses = _dedicated_queue(monkeypatch), []
    monkeypatch.setattr(app, '_is_upload_active', lambda *a: True)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append(a[3]))
    app._run_index_build_job('lib', 'index-build-lib-dedup-idle', 'dedup-idle')
    assert statuses == ['running', 'queued', 'queued'] and table.scans == 0
    assert queue.sent[0]['scope'] == 'dedup-idle' and queue.delays == [int(app.UPLOAD_ACTIVITY_WINDOW_SECONDS)]


def test_idle_fallback_builds_after_crash_and_skips_if_end_refresh_already_covered_it(world, monkeypatch):
    table, _ = world
    activity = HashTable()
    activity.upsert_entity({'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib',
                           'lastUploadActivityAt': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()})
    statuses = []
    monkeypatch.setattr(app, 'metadata_table_client', activity)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append(a[3]))
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda *a: pytest.fail('gallery rebuild'))
    app._run_index_build_job('lib', 'index-build-lib-dedup-idle', 'dedup-idle')
    assert table.scans == 1 and statuses[-1] == 'done'
    app._run_index_build_job('lib', 'index-build-lib-dedup-idle', 'dedup-idle')
    assert table.scans == 1 and statuses[-1] == 'done'
    table.rows['lib', snapshots.SNAPSHOT_ROW]['sourceReadStartedAt'] = (
        datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    # Publication time alone is insufficient if the scan started before the last upload.
    app._run_index_build_job('lib', 'index-build-lib-dedup-idle', 'dedup-idle')
    assert table.scans == 2


def test_dedup_job_never_builds_gallery_indexes(world, monkeypatch):
    status = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: status.append(a[3]))
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda *a: pytest.fail('gallery rebuild'))
    app._run_index_build_job('lib', 'index-build-lib-dedup', 'dedup')
    assert status == ['running', 'done'] and snapshots.read_snapshot('lib')[0] is not None


def test_light_build_queues_dedup_without_inline_scan(world, monkeypatch):
    table, _ = world
    queued = []
    statuses = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append(a[3]))
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda *a: None)
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **kw: [])
    monkeypatch.setattr(su, 'reconcile_search_db', lambda *a: None)
    monkeypatch.setattr(app, '_drain_incremental_search_db', lambda *a: None)
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: queued.append(kw))
    app._run_index_build_job('lib', 'index-build-lib-light', 'light')
    assert table.scans == 0 and statuses[-1] == 'done'
    assert queued == [{'reason': 'index-maintenance', 'scope': 'dedup', 'coalesce': False}]

    def fail_queue(*args, **kwargs):
        raise RuntimeError('queue unavailable')

    monkeypatch.setattr(app, 'enqueue_index_build', fail_queue)
    app._run_index_build_job('lib', 'index-build-lib-light', 'light')
    assert statuses[-1] == 'done'


def test_dedup_queue_scope_has_its_own_coalesced_job(monkeypatch):
    from tests.test_index_priming import _dedicated_queue
    queue = _dedicated_queue(monkeypatch)
    monkeypatch.setattr(app, '_job_row_fresh_active', lambda *a: False)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    assert app.enqueue_index_build('lib', scope='dedup', coalesce=False) == 'queued'
    assert queue.sent[0]['scope'] == 'dedup' and queue.sent[0]['jobId'] == 'index-build-lib-dedup'
    monkeypatch.setattr(app, '_job_row_fresh_active', lambda *a: True)
    assert app.enqueue_index_build('lib', scope='dedup') == 'already_active'
    assert len(queue.sent) == 1


def test_many_hashes_are_losslessly_compressed_and_do_not_rescan_on_reads(world):
    table, _ = world
    for index in range(5000):
        table.upsert_entity({'PartitionKey': 'lib', 'RowKey': hashlib.sha256(str(index).encode()).hexdigest(),
                             'filename': f'IMG_{index:06d}.heic'})
    manifest = snapshots.rebuild_snapshot('lib')
    payload, _, refresh = snapshots.read_snapshot('lib')
    decoded = gzip.decompress(payload)
    assert len(json.loads(decoded)['hashes']) == manifest['rows'] == 5001
    assert len(payload) < len(decoded) * 0.6
    assert table.scans == 1 and not refresh


def test_hash_list_fallback_never_exposes_control_rows(world):
    snapshots.rebuild_snapshot('lib')
    assert su.list_known_file_hashes('lib') == {'a' * 64: 'original.heic'}
