import json

import app
from routes import photos


def test_delete_route_queues_at_async_threshold(monkeypatch):
    queued = []
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', 3)
    monkeypatch.setattr(app, 'BULK_MUTATION_MAX_ITEMS', 10)
    monkeypatch.setattr(
        app,
        '_enqueue_photo_soft_delete_job',
        lambda user_id, names: queued.append((user_id, names)) or {'status': 'queued', 'jobId': 'job-1'},
    )

    with app.app.test_request_context(
        '/api/photos/delete', method='POST',
        json={'filenames': ['a.jpg', 'b.jpg', 'c.jpg']},
    ):
        response, status = photos.delete_multiple_photos()

    assert status == 202
    assert response.get_json()['accepted'] == 3
    assert queued == [('u1', ['a.jpg', 'b.jpg', 'c.jpg'])]


def test_delete_route_keeps_small_mutations_inline(monkeypatch):
    calls = []
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', 3)
    monkeypatch.setattr(app, 'BULK_MUTATION_MAX_ITEMS', 10)
    monkeypatch.setattr(
        app,
        '_soft_delete_photos_now',
        lambda user_id, names: calls.append((user_id, names)) or {
            'deleted': list(names), 'errors': [], 'success': True,
        },
    )

    with app.app.test_request_context(
        '/api/photos/delete', method='POST', json={'filenames': ['a.jpg', 'b.jpg']},
    ):
        response = photos.delete_multiple_photos()

    assert response.status_code == 200
    assert response.get_json()['deleted'] == ['a.jpg', 'b.jpg']
    assert calls == [('u1', ['a.jpg', 'b.jpg'])]


def test_worker_reads_blob_payload_reports_progress_and_cleans_up(monkeypatch):
    names = ['a.jpg', 'b.jpg', 'c.jpg']
    statuses = []
    deleted_blobs = []
    monkeypatch.setattr(
        app,
        'download_file_from_blob',
        lambda container, blob: json.dumps({'filenames': names}).encode(),
    )
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args, **kwargs: statuses.append((args, kwargs)))
    monkeypatch.setattr(
        app,
        '_soft_delete_photos_now',
        lambda user_id, filenames, progress=None: (
            progress(2, 3),
            {'deleted': list(filenames), 'errors': [], 'success': True},
        )[1],
    )
    monkeypatch.setattr(app, '_delete_blob_if_present', lambda container, blob: deleted_blobs.append((container, blob)))

    app._run_photo_soft_delete_job('u1', 'job-1', 'payload.json')

    assert [entry[0][3] for entry in statuses] == ['running', 'running', 'done']
    assert statuses[-1][1]['result'] == {'deleted': 3, 'errors': 0}
    assert deleted_blobs == [(app.BLOB_MERGE_PAYLOADS_CONTAINER, 'payload.json')]


def test_worker_keeps_payload_for_queue_retry(monkeypatch):
    monkeypatch.setattr(
        app,
        'download_file_from_blob',
        lambda container, blob: json.dumps({'filenames': ['a.jpg']}).encode(),
    )
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_soft_delete_photos_now', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    deleted_blobs = []
    monkeypatch.setattr(app, '_delete_blob_if_present', lambda container, blob: deleted_blobs.append(blob))

    try:
        app._run_photo_soft_delete_job('u1', 'job-1', 'payload.json')
        assert False, 'expected worker failure'
    except RuntimeError:
        pass

    assert deleted_blobs == []
