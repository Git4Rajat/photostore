"""Integration-style coverage for the "download entire library" feature.

Exercises the real Flask route functions and the real worker dispatch
function (app._handle_clustering_queue_payload) against a fake Table Storage
client and stubbed blob helpers -- the same pattern test_jobs_status_staleness
uses to run real app.py routes without the full Azure/vision stack.
"""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest

import app


class _FakeTable:
    """Just enough of azure.data.tables to run the library-download code paths:
    arbitrary-partition query_entities, get_entity, and upsert_entity."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError:
            raise Exception('not found')

    def query_entities(self, filter_str):
        import re
        m = re.match(r"PartitionKey eq '([^']*)'$", filter_str)
        assert m, f'unexpected filter: {filter_str}'
        pk = m.group(1)
        return [dict(row) for (p, _), row in self.rows.items() if p == pk]


class _FakeQueue:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, content):
        self.messages.append(json.loads(content))


class _FakeBlob:
    def __init__(self, name):
        self.name = name


class _FakeContainerClient:
    """Just enough of azure.storage.blob's ContainerClient for
    _cleanup_stale_library_export_parts: list_blobs(name_starts_with=...) and
    delete_blob(name)."""

    def __init__(self) -> None:
        self.blobs: dict = {}

    def list_blobs(self, name_starts_with=''):
        return [_FakeBlob(name) for name in self.blobs if name.startswith(name_starts_with)]

    def delete_blob(self, name):
        self.blobs.pop(name, None)


class _FakeBlobServiceClient:
    def __init__(self) -> None:
        self.container = _FakeContainerClient()

    def get_container_client(self, container_name):
        return self.container


@pytest.fixture
def env(monkeypatch):
    table = _FakeTable()
    queue = _FakeQueue()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'blob_service_client', blob_service)
    monkeypatch.setattr(app, '_jobs_partition_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))

    class _FakeLibraryStore:
        def get_library(self, library_id):
            return {'name': 'My Library'}

    monkeypatch.setattr(app, 'library_store', _FakeLibraryStore())
    monkeypatch.setattr(app, '_require_owner_context', lambda *a, **k: ('owner', 'lib1', None))
    monkeypatch.setattr(app, '_require_library_context', lambda *a, **k: ('owner', 'lib1', None))

    # Seed two photo rows for lib1's metadata partition.
    table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': 'photo1.jpg', 'processing_state': 'done'})
    table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': 'photo2.jpg', 'processing_state': 'done'})
    table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': 'photo3.jpg', 'processing_state': 'deleted'})

    monkeypatch.setattr(app, 'resolve_physical_blob_name', lambda lib, fn, kind: fn)
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: b'fake-image-bytes')
    uploaded = {'container': None, 'blobs': {}}

    def _fake_upload(container, blob_name, content, content_type):
        uploaded['container'] = container
        data = content.read() if hasattr(content, 'read') else content
        uploaded['blobs'][blob_name] = data
        blob_service.container.blobs[blob_name] = data

    monkeypatch.setattr(app, 'upload_file_to_blob', _fake_upload)
    monkeypatch.setattr(
        app, '_create_stable_read_sas_url',
        lambda container, name, download_filename=None: (f'https://fake.blob/{container}/{name}?sas=1', '2099-01-01T00:00:00+00:00'),
    )
    return table, queue, uploaded


def test_execute_library_download_builds_zip_and_skips_deleted(env):
    table, _queue, uploaded = env
    result = app._execute_library_download('lib1', 'My Library')

    assert result['photosIncluded'] == 2  # photo3 (deleted) is skipped
    assert result['photosSkipped'] == 0
    assert len(result['parts']) == 1
    part = result['parts'][0]
    assert part['downloadUrl'] == 'https://fake.blob/library-exports/lib1/library-export-part-1.zip?sas=1'
    assert part['photosIncluded'] == 2
    assert uploaded['container'] == app.BLOB_EXPORTS_CONTAINER

    zf = zipfile.ZipFile(io.BytesIO(uploaded['blobs']['lib1/library-export-part-1.zip']))
    assert set(zf.namelist()) == {'photo1.jpg', 'photo2.jpg'}


def test_execute_library_download_heartbeats_job_status(env, monkeypatch):
    """A full-library ZIP export can easily outlast the 15-minute stale-job
    cutoff in /api/jobs/status with no heartbeat in between the initial
    'running' write and the final 'done' write -- that endpoint would then
    force-flip a still-running export to 'failed' ("worker restarted or timed
    out") even though the worker is alive and still writing the ZIP. Verify
    the export refreshes the job row's status/updatedAt while it works."""
    table, _queue, _uploaded = env
    for i in range(30):
        table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': f'extra{i}.jpg', 'processing_state': 'done'})

    running_calls = []
    original = app._upsert_job_status

    def _spy(job_id, user_id, job_type, status, **fields):
        if status == 'running':
            running_calls.append(job_id)
        return original(job_id, user_id, job_type, status, **fields)

    monkeypatch.setattr(app, '_upsert_job_status', _spy)

    result = app._execute_library_download('lib1', 'My Library', job_id='job1', user_id='owner')

    assert result['photosIncluded'] == 32  # 30 extras + photo1/photo2 (photo3 deleted)
    assert running_calls, 'expected at least one heartbeat write during the export'
    row = table.get_entity('jobs', app._job_row_key('job1'))
    assert row['status'] == 'running'


def test_execute_library_download_uses_stored_not_deflated_compression(env):
    """JPEG/HEIC/RAW are already entropy-coded -- DEFLATE just burns CPU for
    near-zero size reduction on this content, competing with the network-bound
    downloads for the same core. Verify parts are written uncompressed."""
    _table, _queue, uploaded = env
    app._execute_library_download('lib1', 'My Library')
    zf = zipfile.ZipFile(io.BytesIO(uploaded['blobs']['lib1/library-export-part-1.zip']))
    assert zf.infolist(), 'expected at least one file in the part'
    for info in zf.infolist():
        assert info.compress_type == zipfile.ZIP_STORED


def test_execute_library_download_downloads_concurrently(env, monkeypatch):
    """Downloads are pure I/O wait -- overlapping several at once instead of
    strictly one-at-a-time is the main lever for real-world throughput.
    Verify concurrency actually happens by timing N artificially slow
    downloads against how long fully sequential processing would take."""
    table, _queue, _uploaded = env
    for i in range(16):
        table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': f'extra{i}.jpg', 'processing_state': 'done'})
    # 18 candidates total (16 extras + photo1/photo2; photo3 stays deleted).
    monkeypatch.setattr(app, 'LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY', 8)

    def _slow_download(kind, name):
        time.sleep(0.1)
        return b'fake-image-bytes'

    monkeypatch.setattr(app, 'download_media_bytes', _slow_download)

    started = time.monotonic()
    result = app._execute_library_download('lib1', 'My Library')
    elapsed = time.monotonic() - started

    assert result['photosIncluded'] == 18
    # Sequential would take ~18 * 0.1s = 1.8s; 8-way concurrency should land
    # near 18/8 * 0.1s ~= 0.25s. Generous bound to avoid flakiness while still
    # catching a regression back to one-at-a-time downloads.
    assert elapsed < 1.2, f'expected concurrent downloads to finish well under sequential time, took {elapsed:.2f}s'


def test_execute_library_download_splits_into_size_capped_parts_and_cleans_up_stale_ones(env, monkeypatch):
    table, _queue, _uploaded = env
    for i in range(4):
        table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': f'extra{i}.jpg', 'processing_state': 'done'})
    # 6 candidate photos total: extra0..3, photo1, photo2.
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: b'x' * 20)
    monkeypatch.setattr(app, 'LIBRARY_EXPORT_PART_MAX_BYTES', 30)  # crosses the cap every 2 photos (40 bytes)

    # A stale part left over from a previous, larger export run must be swept
    # once this run finishes with fewer parts.
    stale_blob = 'lib1/library-export-part-9.zip'
    app.blob_service_client.container.blobs[stale_blob] = b'old'

    result = app._execute_library_download('lib1', 'My Library')

    assert result['photosIncluded'] == 6
    assert result['photosSkipped'] == 0
    assert [p['partIndex'] for p in result['parts']] == [1, 2, 3]
    assert [p['photosIncluded'] for p in result['parts']] == [2, 2, 2]
    assert sum(p['photosIncluded'] for p in result['parts']) == 6
    assert stale_blob not in app.blob_service_client.container.blobs
    assert 'lib1/library-export-part-1.zip' in app.blob_service_client.container.blobs


def test_execute_library_download_resumes_from_durable_checkpoint(env, monkeypatch):
    """Simulates a worker dying mid-export (of the same job_id) after 2 parts
    were durably uploaded, then the queue redelivering the same message. The
    remaining rows should be processed once, not re-downloaded from scratch."""
    table, _queue, _uploaded = env
    for i in range(4):
        table.upsert_entity({'PartitionKey': 'lib1', 'RowKey': f'extra{i}.jpg', 'processing_state': 'done'})
    # 6 candidates, sorted: extra0, extra1, extra2, extra3, photo1, photo2.

    download_calls = []

    def _tracked_download(kind, name):
        download_calls.append(name)
        return b'fake-image-bytes'

    monkeypatch.setattr(app, 'download_media_bytes', _tracked_download)

    job_id = 'job-resume-1'
    app._upsert_job_status(
        job_id, 'owner', 'library_download', 'running', libraryId='lib1',
        exportRowsProcessed=4, exportPhotosWritten=4, exportPhotosSkipped=0,
        exportPartsCompleted=2,
        exportPartsSummary=[
            {'partIndex': 1, 'photosIncluded': 2, 'sizeBytes': 32},
            {'partIndex': 2, 'photosIncluded': 2, 'sizeBytes': 32},
        ],
    )

    result = app._execute_library_download('lib1', 'My Library', job_id=job_id, user_id='owner')

    # Only the 2 rows not yet covered by the durable checkpoint were re-downloaded.
    assert len(download_calls) == 2
    assert download_calls == ['photo1.jpg', 'photo2.jpg']
    assert result['photosIncluded'] == 6
    assert [p['photosIncluded'] for p in result['parts']] == [2, 2, 2]


def test_request_route_enqueues_then_dedupes_on_second_call(env):
    table, queue, _uploaded = env

    with app.app.test_request_context('/api/library/download/request', method='POST'):
        response = app.library_download_request()
    body = response.get_json()
    assert body['status'] == 'queued'
    job_id = body['jobId']
    assert len(queue.messages) == 1
    assert queue.messages[0]['type'] == 'library_download'
    assert queue.messages[0]['libraryId'] == 'lib1'

    # A second click before the job finishes must not enqueue a duplicate.
    with app.app.test_request_context('/api/library/download/request', method='POST'):
        response2 = app.library_download_request()
    body2 = response2.get_json()
    assert body2['jobId'] == job_id
    assert len(queue.messages) == 1  # unchanged


def test_worker_dispatch_and_status_route_end_to_end(env):
    table, _queue, _uploaded = env
    job_id = 'libdownload:lib1:abc123'
    payload = {'jobId': job_id, 'user_id': 'owner', 'libraryId': 'lib1', 'libraryName': 'My Library', 'type': 'library_download'}

    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'library_download')

    with app.app.test_request_context(f'/api/library/download/status?jobId={job_id}'):
        response = app.library_download_status()
    body = response.get_json()
    assert body['status'] == 'done'
    assert body['result']['photosIncluded'] == 2
    assert len(body['result']['parts']) == 1
    assert body['result']['parts'][0]['downloadUrl'].startswith('https://fake.blob/')


def test_humanize_job_for_library_download():
    done_row = {'jobType': 'library_download', 'status': 'done', 'result': json.dumps({'photosIncluded': 5, 'parts': [{'partIndex': 1}]})}
    humanized = app._humanize_job(done_row)
    assert humanized['kind'] == 'library_download'
    assert humanized['title'] == 'Library export ready'
    assert '5 photos' in humanized['message']
    assert 'part' not in humanized['message']  # single part -- no part count mentioned

    multi_part_row = {'jobType': 'library_download', 'status': 'done', 'result': json.dumps({'photosIncluded': 50, 'parts': [{'partIndex': 1}, {'partIndex': 2}, {'partIndex': 3}]})}
    humanized_multi = app._humanize_job(multi_part_row)
    assert '3 parts' in humanized_multi['message']

    failed_row = {'jobType': 'library_download', 'status': 'failed', 'error': 'boom'}
    humanized_failed = app._humanize_job(failed_row)
    assert humanized_failed['title'] == 'Library export failed'
    assert humanized_failed['message'] == 'boom'
