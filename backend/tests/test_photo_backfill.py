"""Library backfill must be bounded and must not claim failed sends succeeded."""
import pytest

import app
from routes import admin


@pytest.fixture
def backfill(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'PROCESSING_MODE', 'backend')
    monkeypatch.setattr(app, 'ipwork_queue_client', object())
    monkeypatch.setattr(app, '_enqueue_processing_steps', lambda uid, name, steps, **kw: {s: {'status': 'queued'} for s in steps})
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **kw: {'status': 'queued'})
    calls = []

    def scan(uid, **kwargs):
        calls.append((uid, kwargs))
        return iter([{'RowKey': f'{i:03}.jpg'} for i in range(25)])

    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', scan)

    def invoke(**body):
        with app.app.test_request_context('/api/admin/backfill/photos', method='POST', json={
            'repair': True, 'confirm': 'BACKFILL_ALL_PHOTOS', **body,
        }):
            response = admin.admin_backfill_photos()
        if isinstance(response, tuple):
            return response[0].get_json(), response[1]
        return response.get_json(), 200

    return invoke, calls


def test_backfill_is_bounded_and_returns_cursor(backfill, monkeypatch):
    invoke, calls = backfill
    names = []
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda uid, name, **kw: names.append(name) or {'status': 'queued'})
    payload, status = invoke()
    assert status == 200
    assert payload['queued'] == 10 and payload['total'] == 10
    assert payload['complete'] is False and payload['continuation'] == '009.jpg'
    assert names == [f'{i:03}.jpg' for i in range(10)]
    assert calls[0][1]['select'] == ['RowKey', 'processing_state']
    assert calls[0][1]['page_size'] == 11
    invoke(continuation="009'photo.jpg")
    assert calls[1][1]['extra_filter'] == "RowKey gt '009''photo.jpg'"


def test_continuation_visits_each_photo_exactly_once(backfill, monkeypatch):
    invoke, _ = backfill
    rows = [{'RowKey': f'{i:03}.jpg'} for i in range(25)]
    sent = []

    def scan(uid, **kwargs):
        expression = kwargs['extra_filter']
        cursor = expression[len("RowKey gt '"):-1] if expression else ''
        return (row for row in rows if row['RowKey'] > cursor)

    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', scan)
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda uid, name, **kw: sent.append(name) or {'status': 'queued'})
    cursor = None
    counts = []
    while True:
        payload, status = invoke(**({'continuation': cursor} if cursor else {}))
        assert status == 200
        counts.append(payload['queued'])
        if payload['complete']:
            break
        cursor = payload['continuation']
    assert counts == [10, 10, 5]
    assert sent == [row['RowKey'] for row in rows]


def test_final_batch_skips_videos_and_deleted(backfill, monkeypatch):
    invoke, _ = backfill
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **kw: iter([
        {'RowKey': 'a.jpg'}, {'RowKey': 'b.mp4'}, {'RowKey': 'c.jpg', 'processing_state': 'deleted'},
    ]))
    payload, status = invoke(steps=['ocr', 'ocr'])
    assert status == 200
    assert payload['queued'] == 1 and payload['skipped'] == 2 and payload['failed'] == 0
    assert payload['complete'] is True and payload['continuation'] is None
    assert payload['steps'] == ['ocr']


@pytest.mark.parametrize('queue_status', ['failed', 'unavailable'])
def test_queue_failure_is_not_counted_as_queued_or_skipped(backfill, monkeypatch, queue_status):
    invoke, _ = backfill
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **kw: {'status': queue_status})
    payload, status = invoke()
    assert status == 200
    assert payload['queued'] == payload['skipped'] == 0
    assert payload['failed'] == payload['total'] == 10


def test_unavailable_backend_queue_does_not_reset_metadata(backfill, monkeypatch):
    invoke, calls = backfill
    monkeypatch.setattr(app, 'ipwork_queue_client', None)
    payload, status = invoke()
    assert status == 503 and payload['code'] == 'ipwork_unavailable'
    assert calls == []


def test_browser_mode_accepts_queue_noop(backfill, monkeypatch):
    invoke, _ = backfill
    monkeypatch.setattr(app, 'PROCESSING_MODE', 'browser')
    monkeypatch.setattr(app, 'ipwork_queue_client', None)
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **kw: {'status': 'skipped'})
    payload, status = invoke()
    assert status == 200 and payload['queued'] == 10 and payload['processingMode'] == 'browser'


def test_missing_photo_is_not_sent_to_worker(backfill, monkeypatch):
    invoke, _ = backfill
    monkeypatch.setattr(app, '_enqueue_processing_steps', lambda *a, **kw: {'ocr': {'status': 'error', 'reason': 'not found'}})
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **kw: pytest.fail('must not send missing photo'))
    payload, _ = invoke(steps=['ocr'])
    assert payload['queued'] == 0 and payload['failed'] == 10


def test_lazy_scan_failure_returns_json_and_partial_counts(backfill, monkeypatch):
    invoke, _ = backfill

    def scan(*args, **kwargs):
        yield {'RowKey': 'a.jpg'}
        raise RuntimeError('page unavailable')

    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', scan)
    payload, status = invoke()
    assert status == 503 and payload['queued'] == 1 and payload['complete'] is False


@pytest.mark.parametrize('cursor', ['', 12, [], 'x' * 1025])
def test_invalid_cursor_is_rejected(backfill, cursor):
    invoke, calls = backfill
    payload, status = invoke(continuation=cursor)
    assert status == 400 and payload['code'] == 'invalid_continuation' and calls == []