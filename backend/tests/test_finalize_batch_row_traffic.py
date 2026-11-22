"""A finalize-batch file touches its photo row as few times as it can.

Measured on the QA library (10k small photos, 2026-11-22) a finished file cost about 47 storage calls,
most of them reads and rewrites of the one photo row: finalize wrote it, the stamp, the client results,
the metadata packet and the receipt each read it again and wrote it again. These tests count the row's
calls through the real route and pin the result, and check the cases that make reusing a row unsafe.
"""
import collections
import json
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceModifiedError
from azure.data.tables import TableEntity

import app
import storage_utils
import wave_policy
from fakes import FakeTable
from routes import upload

SHA = 'a' * 64
PACKET = {'version': 1, 'bytes': 64, 'sha256': 'b' * 64, 'sourceSha256': SHA}
REPORT = {'version': 1, 'assets': {'preview': {'generation': 'generated', 'upload': 'uploaded', 'target': True},
                                   'thumbnail': {'generation': 'generated', 'upload': 'uploaded', 'target': True}}}


class Counting:
    """Counts the calls a table gets, by method."""
    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in ('get_entity', 'update_entity', 'upsert_entity', 'create_entity', 'query_entities'):
            return attr

        def counted(*args, **kwargs):
            self._calls[name] += 1
            return attr(*args, **kwargs)
        return counted


@pytest.fixture
def traffic(monkeypatch):
    calls = collections.Counter()
    table = FakeTable()
    counted = Counting(table, calls)
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', counted)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', FakeTable())
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbnails')
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda *args: SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(
        size=PACKET['bytes'], content_settings=SimpleNamespace(content_type='application/octet-stream'), etag='packet-etag')))
    monkeypatch.setattr(app, 'metadata_table_client', counted)
    monkeypatch.setattr(app, '_require_library_context', lambda *a, **k: ('account', 'library', None))
    monkeypatch.setattr(app, '_library_cleanup_block_reason', lambda *_: None)
    for name in ('_mark_fresh_upload_activity', '_mark_upload_activity_heartbeat',
                 '_queue_people_clustering_after_face_processing', 'enqueue_index_build', '_queue_upload_processing'):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_validate_client_blob_name', lambda value: value)
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kw: SimpleNamespace(
        get_blob_properties=lambda: SimpleNamespace(size=100))))
    return SimpleNamespace(table=table, calls=calls)


def item(i, **extra):
    return {'filename': f'p{i}.jpg', 'blobName': f'physical{i}', 'uploadId': f'id{i}', 'totalSize': 100, 'sha256': f'{i:02x}' * 32,
            'clientAssetId': f'ddf07989-d788-4ecb-a8b5-94bf671771a{i}',
            'clientProcessing': {'exif': {'source': 'browser_exiftool', 'verifiedComplete': True,
                                          'exifToolJson': {'EXIF:Make': 'Test'}}},
            'clientProcessingReport': [{'step': 'exif', 'status': 'done', 'reason': 'done', 'durationMs': 5,
                                        'runtime': 'browser-exiftool-wasm'}],
            'clientMetadata': dict(PACKET, sourceSha256=f'{i:02x}' * 32),
            'preparationReport': REPORT, **extra}


def finalize(files):
    with app.app.test_request_context('/upload/finalize-batch', method='POST', json={'files': files}):
        response = upload.finalize_upload_batch()
    assert response.status_code == 200
    results = response.get_json()['results']
    assert all(result.get('complete') for result in results), results
    return results


def reserve(traffic, files):
    """The row /upload/init-batch leaves behind: the physical blob reserved under the filename."""
    for f in files:
        traffic.table.upsert_entity({'PartitionKey': 'library', 'RowKey': f['filename'], 'pendingAnonymousBlob': f['blobName'],
                                     'upload_sha256_expected': f['sha256']})


def row_traffic(calls, files):
    return {name: calls[name] / len(files) for name in ('get_entity', 'update_entity', 'upsert_entity', 'create_entity')}


def test_each_file_reads_its_row_once_and_rewrites_it_four_times(traffic):
    files = [item(i) for i in range(5)]
    reserve(traffic, files)
    traffic.calls.clear()
    finalize(files)
    per_file = row_traffic(traffic.calls, files)
    # Before: 6 reads (finalize, stamp, client results, packet check, packet write, receipt) and 5 writes
    # (finalize, stamp, client results, packet, receipt).
    assert per_file['get_entity'] == 1
    assert per_file['update_entity'] + per_file['upsert_entity'] + per_file['create_entity'] == 4


def test_the_packet_and_the_receipt_are_stored_in_one_write(traffic):
    files = [item(1)]
    reserve(traffic, files)
    finalize(files)
    row = traffic.table.rows[('library', 'p1.jpg')]
    assert json.loads(row['uploadMetadataPacket'])['physical'] == 'physical1'
    receipt = json.loads(row['uploadPreparationReport'])['backendReceipt']
    assert receipt['metadata'] == 'accepted'
    assert row['size'] == 100 and row['uploadedBy'] == 'account' and row['fileHash'] == files[0]['sha256']
    assert json.loads(row['exifData'])['Make'] == 'Test'


def test_a_packet_without_a_receipt_is_still_stored(traffic):
    files = [item(2, preparationReport=None)]
    reserve(traffic, files)
    traffic.calls.clear()
    finalize(files)
    row = traffic.table.rows[('library', 'p2.jpg')]
    assert json.loads(row['uploadMetadataPacket'])['physical'] == 'physical2'
    assert 'uploadPreparationReport' not in row
    assert traffic.calls['get_entity'] == 1


def test_a_rejected_packet_leaves_the_receipt_saying_so(traffic, monkeypatch):
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda *args: SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(
        size=1, content_settings=SimpleNamespace(content_type='application/octet-stream'), etag='x')))
    files = [item(3)]
    reserve(traffic, files)
    finalize(files)
    row = traffic.table.rows[('library', 'p3.jpg')]
    assert 'uploadMetadataPacket' not in row
    assert json.loads(row['uploadPreparationReport'])['backendReceipt']['metadata'] == 'rejected'


def test_the_fence_is_read_once_per_file_before_its_write(traffic):
    fences = []
    token = wave_policy.begin_finalization(lambda: fences.append(1))
    try:
        files = [item(i) for i in range(3)]
        reserve(traffic, files)
        finalize(files)
    finally:
        wave_policy.end_finalization(token)
    # One read per file, immediately before the write it guards. It used to be read again at the start.
    assert len(fences) == 3


def test_a_duplicate_is_still_answered_with_the_stored_photo_and_does_not_restart_its_processing(traffic):
    files = [item(4)]
    reserve(traffic, files)
    finalize(files)
    before = dict(traffic.table.rows[('library', 'p4.jpg')])
    again = item(4, filename='again.jpg')
    reserve(traffic, [again])
    results = finalize([again])
    assert results[0]['duplicates'] and results[0]['filename'] == 'p4.jpg'
    row = traffic.table.rows[('library', 'p4.jpg')]
    for field in ('fileHash', 'processingRevision', 'processingGeneration', 'size', 'exifData', 'processing_state'):
        assert row.get(field) == before.get(field)


def test_a_row_changed_by_someone_else_in_between_is_not_overwritten(traffic):
    """The ETag we hold is stale: the conditional write fails and the update re-reads and keeps their change."""
    table = traffic.table
    table.upsert_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'processing_state': 'active', 'rating': 0})
    held = TableEntity(table.get_entity('library', 'p.jpg'))
    held._metadata = {'etag': str(table.versions[('library', 'p.jpg')]), 'timestamp': None}
    table.update_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'rating': 5}, mode='merge')
    traffic.calls.clear()
    updated = app._update_metadata_entity_fields('library', 'p.jpg', {'size': 100}, initial_entity=held)
    row = table.rows[('library', 'p.jpg')]
    assert row['rating'] == 5 and row['size'] == 100
    assert updated['size'] == 100
    assert traffic.calls['get_entity'] == 1  # the one re-read after the lost race


def test_an_update_hands_back_the_row_with_the_etag_of_its_write(traffic):
    table = traffic.table
    table.upsert_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'processing_state': 'active'})
    first = app._update_metadata_entity_fields('library', 'p.jpg', {'size': 1})
    second = app._update_metadata_entity_fields('library', 'p.jpg', {'size': 2}, initial_entity=first)
    assert second['size'] == 2 and table.rows[('library', 'p.jpg')]['size'] == 2
    assert first.metadata['etag'] == str(table.versions[('library', 'p.jpg')] - 1)


def test_no_etag_is_handed_back_when_publishing_the_index_rewrites_the_row(traffic, monkeypatch):
    """Clearing the index outbox rewrites the row, so the write's ETag is stale the moment the update returns."""
    table = traffic.table
    table.upsert_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'processing_state': 'active'})
    monkeypatch.setattr(storage_utils, 'prepare_metadata_index_intent', lambda entity, changed: entity.update(indexPending='{"kinds":["sort"]}'))
    updated = app._update_metadata_entity_fields('library', 'p.jpg', {'size': 1})
    assert updated['indexPending'] and not updated.metadata.get('etag')


def test_client_results_start_from_the_row_the_caller_holds(traffic):
    table = traffic.table
    table.upsert_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'processing_state': 'active', 'processing_metadata': '{}',
                         'exif_status': 'pending', 'map_detection_status': 'pending'})
    held = TableEntity(table.get_entity('library', 'p.jpg'))
    held._metadata = {'etag': str(table.versions[('library', 'p.jpg')]), 'timestamp': None}
    traffic.calls.clear()
    result = storage_utils.apply_client_processing_results_for_file(
        'library', 'p.jpg', client_processing={'exif': {'source': 'browser_exiftool', 'verifiedComplete': True,
                                                          'exifToolJson': {'EXIF:Make': 'Test'}}},
        client_processing_report=[{'step': 'exif', 'status': 'done', 'reason': 'done', 'durationMs': 5, 'runtime': 'browser-exiftool-wasm'}],
        initial_entity=held)
    assert traffic.calls['get_entity'] == 0
    assert json.loads(table.rows[('library', 'p.jpg')]['exifData'])['Make'] == 'Test'
    assert result.metadata['etag'] == str(table.versions[('library', 'p.jpg')])


def test_client_results_never_overwrite_a_row_changed_since_the_one_held(traffic):
    """Holds for a row read the usual way too; what the commit does with its own results after losing the race is
    unchanged by starting from a held row."""
    table = traffic.table
    table.upsert_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'processing_state': 'active', 'processing_metadata': '{}',
                         'exif_status': 'pending', 'map_detection_status': 'pending', 'rating': 0})
    held = TableEntity(table.get_entity('library', 'p.jpg'))
    held._metadata = {'etag': str(table.versions[('library', 'p.jpg')]), 'timestamp': None}
    table.update_entity({'PartitionKey': 'library', 'RowKey': 'p.jpg', 'rating': 4}, mode='merge')
    storage_utils.apply_client_processing_results_for_file(
        'library', 'p.jpg', client_processing={'exif': {'source': 'browser_exiftool', 'verifiedComplete': True,
                                                          'exifToolJson': {'EXIF:Make': 'Test'}}},
        client_processing_report=[{'step': 'exif', 'status': 'done', 'reason': 'done', 'durationMs': 5, 'runtime': 'browser-exiftool-wasm'}],
        initial_entity=held)
    # The conditional commit lost the race: the other writer's change is kept, not overwritten.
    assert table.rows[('library', 'p.jpg')]['rating'] == 4


def test_the_row_written_by_finalize_carries_its_etag():
    table = FakeTable()
    written = table.upsert_entity({'PartitionKey': 'p', 'RowKey': 'r', 'a': 1})
    row = storage_utils.row_after_write({'PartitionKey': 'p', 'RowKey': 'r', 'a': 1}, written)
    assert row.metadata['etag'] == '1' and row['a'] == 1
    # A write that reports no ETag leaves the row as it was: the next writer reads.
    plain = {'PartitionKey': 'p', 'RowKey': 'r'}
    assert storage_utils.row_after_write(plain, None) is plain
    assert not getattr(storage_utils.row_after_write(plain, {}), 'metadata', {}).get('etag')
