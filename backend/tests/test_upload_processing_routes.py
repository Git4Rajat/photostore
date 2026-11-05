"""Browser-processing lease routes: pending, claim, claim-batch, heartbeat, release, cancel."""
import pytest

import app


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()


def _pending_rows(monkeypatch, rows):
    monkeypatch.setattr(app, 'metadata_table_client', object())
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter(rows))
    monkeypatch.setattr(app, '_browser_processing_pending_item', lambda row: dict(row) if row.get('pending') else None)
    monkeypatch.setattr(app, '_create_scoped_blob_url', lambda container, name, minutes=10: (f'https://blob/{name}', 'exp'))
    monkeypatch.setattr(app, '_create_direct_thumbnail_upload_blob_url', lambda name: (f'https://thumb/{name}', 'texp'))


def test_pending_is_empty_until_metadata_is_configured(http, monkeypatch):
    monkeypatch.setattr(app, 'metadata_table_client', None)
    assert http.get('/upload/processing/pending').json == {'pending': []}


def test_pending_rejects_a_non_numeric_limit(http, monkeypatch):
    _pending_rows(monkeypatch, [])
    assert http.get('/upload/processing/pending?limit=abc').status_code == 400


def test_pending_returns_oldest_first_bounded_with_signed_urls(http, monkeypatch):
    rows = [
        {'filename': 'new.jpg', 'pending': True, 'lastProcessingUpdate': '2026-02', '_blobName': 'anon-new'},
        {'filename': 'old.jpg', 'pending': True, 'lastProcessingUpdate': '2026-01', '_blobName': 'anon-old'},
        {'filename': 'done.jpg', 'pending': False},
    ]
    _pending_rows(monkeypatch, rows)
    body = http.get('/upload/processing/pending?limit=1').json
    assert [i['filename'] for i in body['pending']] == ['old.jpg']
    assert body['totalPending'] == 2
    item = body['pending'][0]
    assert item['sourceUrl'] == 'https://blob/anon-old' and item['thumbnailUploadUrl'] == 'https://thumb/anon-old'
    assert '_blobName' not in item


def test_pending_clamps_limit_to_sixty(http, monkeypatch):
    rows = [{'filename': f'{i}.jpg', 'pending': True, 'lastProcessingUpdate': str(i)} for i in range(70)]
    _pending_rows(monkeypatch, rows)
    assert len(http.get('/upload/processing/pending?limit=500').json['pending']) == 60


def test_pending_survives_a_failed_scan_and_a_failed_signature(http, monkeypatch):
    monkeypatch.setattr(app, 'metadata_table_client', object())
    def boom(*a, **k):
        raise RuntimeError('table down')
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', boom)
    assert http.get('/upload/processing/pending').json == {'pending': []}
    _pending_rows(monkeypatch, [{'filename': 'a.jpg', 'pending': True}])
    monkeypatch.setattr(app, '_create_scoped_blob_url', boom)
    item = http.get('/upload/processing/pending').json['pending'][0]
    assert 'sourceUrl' not in item and item['thumbnailUploadUrl']


def test_claim_requires_a_filename_and_forwards_steps_and_owner(http, monkeypatch):
    seen = {}
    def fake_claim(user, filename, owner, steps, blob):
        seen.update(user=user, filename=filename, owner=owner, steps=steps, blob=blob)
        return {'claimed': True, 'leaseId': owner}, 200
    monkeypatch.setattr(app, '_claim_processing_lease_response', fake_claim)
    assert http.post('/upload/processing/claim', json={}).status_code == 400
    response = http.post('/upload/processing/claim', json={'filename': 'a.jpg', 'leaseId': 'L1', 'steps': ['exif', ' ocr '], 'blobName': 'b'})
    assert response.status_code == 200 and response.json['claimed']
    assert seen == {'user': 'u1', 'filename': 'a.jpg', 'owner': 'L1', 'steps': ['exif', 'ocr'], 'blob': 'b'}


def test_claim_generates_an_owner_and_passes_the_helper_status_through(http, monkeypatch):
    seen = {}
    monkeypatch.setattr(app, '_claim_processing_lease_response', lambda u, f, owner, s, b: (seen.update(owner=owner, steps=s) or {'claimed': False, 'reason': 'busy'}, 409))
    response = http.post('/upload/processing/claim', json={'filename': 'a.jpg'})
    assert response.status_code == 409 and response.json['reason'] == 'busy'
    assert seen['owner'].startswith('browser-') and seen['steps'] is None


def test_claim_batch_validates_then_reports_each_item_independently(http, monkeypatch):
    monkeypatch.setattr(app, '_claim_processing_lease_response', lambda u, f, owner, s, b: ({'claimed': f != 'busy.jpg'}, 200))
    assert http.post('/upload/processing/claim-batch', json={'items': []}).status_code == 400
    assert http.post('/upload/processing/claim-batch', json={'items': 'x'}).status_code == 400
    too_many = [{'filename': 'a.jpg'}] * (app.MAX_CLAIM_BATCH_ITEMS + 1)
    assert http.post('/upload/processing/claim-batch', json={'items': too_many}).status_code == 400
    results = http.post('/upload/processing/claim-batch', json={'items': ['junk', {'filename': ''}, {'filename': 'ok.jpg'}, {'filename': 'busy.jpg'}]}).json['results']
    assert results[0] == {'claimed': False, 'reason': 'invalid_item'}
    assert results[1] == {'claimed': False, 'reason': 'invalid_filename'}
    assert results[2]['filename'] == 'ok.jpg' and results[2]['claimed']
    assert results[3]['filename'] == 'busy.jpg' and not results[3]['claimed']


def test_heartbeat_extends_or_reports_a_missing_lease(http, monkeypatch):
    monkeypatch.setattr(app, 'heartbeat_processing_lease', lambda u, f, lease, lease_seconds, **kw: {'leaseExpiresAt': 'T+120'})
    ok = http.post('/upload/processing/heartbeat', json={'filename': 'a.jpg', 'leaseId': 'L1'})
    assert ok.json == {'ok': True, 'expiresAt': 'T+120'}
    def gone(*a, **k):
        raise KeyError('lease')
    monkeypatch.setattr(app, 'heartbeat_processing_lease', gone)
    lost = http.post('/upload/processing/heartbeat', json={'filename': 'a.jpg', 'leaseId': 'L1'})
    assert lost.status_code == 409 and lost.json['reason'] == 'lease_missing'


def test_heartbeat_forwards_the_processing_fence(http, monkeypatch):
    seen = {}
    monkeypatch.setattr(app, 'heartbeat_processing_lease', lambda u, f, lease, lease_seconds, **kw: seen.update(kw) or {})
    http.post('/upload/processing/heartbeat', json={'filename': 'a.jpg', 'leaseId': 'L1', 'processingFence': 7})
    assert seen == {'expected_processing_fence': 7}


def test_release_requires_filename_and_lease_and_forwards_the_fence(http, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'release_processing_lease', lambda u, f, lease, **kw: calls.append((u, f, lease, kw)))
    assert http.post('/upload/processing/release', json={'filename': 'a.jpg'}).status_code == 400
    assert http.post('/upload/processing/release', json={'leaseId': 'L1'}).status_code == 400
    assert not calls
    assert http.post('/upload/processing/release', json={'filename': 'a.jpg', 'leaseId': 'L1', 'processingFence': 3}).json == {'ok': True}
    assert calls == [('u1', 'a.jpg', 'L1', {'expected_processing_fence': 3})]


def test_cancel_validates_the_payload(http):
    assert http.post('/upload/cancel', json={'files': []}).status_code == 400
    assert http.post('/upload/cancel', json={'files': 'a.jpg'}).status_code == 400


def test_cancel_cleans_valid_files_and_reports_invalid_ones_as_partial(http, monkeypatch):
    cleaned = []
    monkeypatch.setattr(app, '_cleanup_failed_upload', lambda u, name, upload_id: cleaned.append((name, upload_id)) or {'filename': name, 'errors': []})
    ok = http.post('/upload/cancel', json={'files': [{'filename': 'a.jpg', 'uploadId': 'U1'}]})
    assert ok.status_code == 200 and ok.json['success'] and cleaned == [('a.jpg', 'U1')]
    partial = http.post('/upload/cancel', json={'files': [{'filename': 'b.jpg'}, 'junk', {'filename': ''}]})
    assert partial.status_code == 207 and not partial.json['success']
    assert [e['error'] for e in partial.json['errors']] == ['Invalid file entry', 'Invalid filename']


def test_cancel_surfaces_cleanup_errors_as_partial(http, monkeypatch):
    monkeypatch.setattr(app, '_cleanup_failed_upload', lambda u, name, upload_id: {'filename': name, 'errors': ['blob busy', 'row busy']})
    response = http.post('/upload/cancel', json={'files': [{'filename': 'a.jpg'}]})
    assert response.status_code == 207 and response.json['errors'] == [{'filename': 'a.jpg', 'error': 'blob busy; row busy'}]
