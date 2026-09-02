"""Integration-style coverage for the "download entire library" feature.

Exercises the real Flask route functions and the real worker dispatch
function (app._handle_clustering_queue_payload) against a fake Table Storage
client and stubbed blob helpers -- the same pattern test_jobs_status_staleness
uses to run real app.py routes without the full Azure/vision stack.
"""
from __future__ import annotations

import json

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


@pytest.fixture
def env(monkeypatch):
    table = _FakeTable()
    queue = _FakeQueue()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
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
    uploaded = {}

    def _fake_upload(container, blob_name, content, content_type):
        uploaded['container'] = container
        uploaded['blob_name'] = blob_name
        uploaded['bytes'] = content.read() if hasattr(content, 'read') else content

    monkeypatch.setattr(app, 'upload_file_to_blob', _fake_upload)
    monkeypatch.setattr(app, 'uploaded_export', uploaded, raising=False)
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
    assert result['downloadUrl'] == 'https://fake.blob/library-exports/lib1/library-export.zip?sas=1'
    assert uploaded['container'] == app.BLOB_EXPORTS_CONTAINER
    assert uploaded['blob_name'] == 'lib1/library-export.zip'

    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(uploaded['bytes']))
    assert set(zf.namelist()) == {'photo1.jpg', 'photo2.jpg'}


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
    assert body['result']['downloadUrl'].startswith('https://fake.blob/')


def test_humanize_job_for_library_download():
    done_row = {'jobType': 'library_download', 'status': 'done', 'result': json.dumps({'photosIncluded': 5})}
    humanized = app._humanize_job(done_row)
    assert humanized['kind'] == 'library_download'
    assert humanized['title'] == 'Library export ready'
    assert '5 photos' in humanized['message']

    failed_row = {'jobType': 'library_download', 'status': 'failed', 'error': 'boom'}
    humanized_failed = app._humanize_job(failed_row)
    assert humanized_failed['title'] == 'Library export failed'
    assert humanized_failed['message'] == 'boom'
