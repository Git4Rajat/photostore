"""Grid images: the sharp srcset tile made from each photo's preview (grid_images.py)."""
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError
from PIL import Image

import app
import grid_images
import image_utils
import storage_utils


def _jpeg(size, *, orientation=1, color='teal'):
    image = Image.new('RGB', size, color)
    exif = Image.Exif()
    exif[0x0112] = orientation
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=85, exif=exif)
    return out.getvalue()


def _row(**fields):
    row = {'PartitionKey': 'lib', 'RowKey': 'IMG_1.jpg', 'anonymousImageId': 'abc123',
           'preview_status': 'done', 'uploadDate': '2026-01-01T00:00:00+00:00'}
    row.update(fields)
    return row


def _stamped(**fields):
    record = grid_images.record(grid_images.blob_name('abc123'), 600, 400, 30000, 'preview/abc123.jpg')
    return _row(gridImage=record, **fields)


# ---- encoder --------------------------------------------------------------


@pytest.mark.parametrize('orientation,expected', [(1, (600, 400)), (3, (600, 400)), (6, (400, 600)), (8, (400, 600))])
def test_grid_image_covers_a_square_tile_with_its_short_edge(orientation, expected):
    data, width, height = image_utils.create_grid_image_data(_jpeg((2048, 1365), orientation=orientation))
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == 'WEBP'
        assert image.size == expected == (width, height)
        assert min(image.size) == image_utils.GRID_SHORT_EDGE


def test_grid_image_never_upscales_and_caps_panoramas():
    assert image_utils.create_grid_image_data(_jpeg((300, 200)))[1:] == (300, 200)
    assert image_utils.create_grid_image_data(_jpeg((2048, 512)))[1:] == (image_utils.GRID_MAX_LONG_EDGE, 200)


def test_grid_image_rejects_undecodable_previews():
    with pytest.raises(Exception):
        image_utils.create_grid_image_data(b'not an image')


# ---- record and eligibility ------------------------------------------------


def test_served_blob_only_returns_this_photos_record():
    assert grid_images.served_blob(_stamped()) == 'grid/abc123.webp'
    assert grid_images.served_blob(_row()) == ''
    assert grid_images.served_blob(_stamped(anonymousImageId='someone-else')) == ''
    assert grid_images.served_blob(_row(gridImage='{not json')) == ''
    assert grid_images.served_blob(_row(gridImage=json.dumps({'v': 99, 'blob': 'grid/abc123.webp'}))) == ''
    assert grid_images.served_blob(None) == ''


def test_needs_grid_picks_finished_photos_with_a_preview_and_no_grid_image():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert grid_images.needs_grid(_row(), now=now)
    assert not grid_images.needs_grid(_stamped(), now=now)
    assert not grid_images.needs_grid(_row(preview_status='pending'), now=now)
    assert not grid_images.needs_grid(_row(RowKey='clip.mov'), now=now)
    assert not grid_images.needs_grid(_row(processing_state='deleted'), now=now)
    # An unfinished upload reservation is not a photo yet.
    assert not grid_images.needs_grid({'RowKey': 'IMG_2.jpg', 'preview_status': 'done',
                                       'pendingAnonymousBlob': 'x'}, now=now)


def test_needs_grid_leaves_photos_under_a_live_processing_lease_alone():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    live = (now + timedelta(minutes=5)).isoformat()
    expired = (now - timedelta(minutes=5)).isoformat()
    assert not grid_images.needs_grid(_row(processing_lease_owner='ipworker-1', processing_lease_expires_at=live), now=now)
    assert grid_images.needs_grid(_row(processing_lease_owner='ipworker-1', processing_lease_expires_at=expired), now=now)


def test_stamp_pins_the_upload_date_of_legacy_rows_before_rewriting_last_processing_update():
    stamp = 'record'
    assert grid_images.stamp_updates(_row(), stamp) == {'gridImage': stamp}
    legacy = {'RowKey': 'old.jpg', 'last_processing_update': '2020-05-01T00:00:00+00:00'}
    assert grid_images.stamp_updates(legacy, stamp) == {'gridImage': stamp, 'uploadDate': '2020-05-01T00:00:00+00:00'}
    started = dict(legacy, upload_started_at='2020-04-30T00:00:00+00:00')
    assert grid_images.stamp_updates(started, stamp) == {'gridImage': stamp}


def test_job_message_drops_malformed_since_values():
    assert json.loads(grid_images.job_message('lib', since='2026-11-11T10:00:00Z'))['since'] == '2026-11-11T10:00:00Z'
    assert json.loads(grid_images.job_message('lib', since="x' or true"))['since'] == ''
    assert grid_images.since_from_epoch(1_800_000_000) == '2027-01-15T07:55:00Z'
    assert grid_images.since_from_epoch(None) == ''


# ---- batching --------------------------------------------------------------


def _rows(n):
    return [_row(RowKey=f'IMG_{i:03}.jpg', anonymousImageId=f'id{i}') for i in range(n)]


def test_run_batch_stops_at_the_limit_and_returns_a_cursor():
    done = []
    result = grid_images.run_batch(_rows(5), lambda row: done.append(row['RowKey']) or True, limit=2, workers=1)
    assert sorted(done) == ['IMG_000.jpg', 'IMG_001.jpg']
    assert result == {'scanned': 2, 'made': 2, 'failed': 0, 'cursor': 'IMG_001.jpg'}


def test_run_batch_skips_finished_photos_and_reports_completion():
    rows = [_stamped(RowKey='a.jpg'), _row(RowKey='b.jpg'), _row(RowKey='c.mov')]
    result = grid_images.run_batch(rows, lambda row: True, limit=10)
    assert result == {'scanned': 3, 'made': 1, 'failed': 0, 'cursor': ''}


def test_run_batch_yields_when_its_time_budget_runs_out():
    ticks = iter([0, 1, 100, 200])
    result = grid_images.run_batch(_rows(5), lambda row: True, limit=10, budget_seconds=50, clock=lambda: next(ticks))
    assert result['cursor'] == 'IMG_001.jpg' and result['made'] == 2


def test_run_batch_counts_failures_without_stopping():
    def process(row):
        if row['RowKey'] == 'IMG_001.jpg':
            raise OSError('blob unavailable')
        return row['RowKey'] != 'IMG_002.jpg'
    result = grid_images.run_batch(_rows(4), process, limit=10)
    assert result == {'scanned': 4, 'made': 2, 'failed': 2, 'cursor': ''}


# ---- serving -----------------------------------------------------------------


def test_sort_index_row_offers_the_grid_image_only_once_it_exists():
    assert 'grid' not in storage_utils._sort_index_row(_row())
    assert storage_utils._sort_index_row(_stamped())['grid'] == 'grid/abc123.webp'
    assert 'gridImage' in storage_utils._SORT_INDEX_SOURCE_FIELDS
    assert 'sort' in storage_utils.metadata_index_kinds({'gridImage': 'x'})


def test_photo_summary_carries_the_grid_blob_in_token_mode(monkeypatch):
    monkeypatch.setattr(app, '_direct_media_requested', lambda: True)
    summary = app._build_photo_summary('lib', 'IMG_1.jpg', _stamped(), include_props=False)
    assert summary['gridBlob'] == 'grid/abc123.webp'
    assert 'gridBlob' not in app._build_photo_summary('lib', 'IMG_1.jpg', _row(), include_props=False)
    monkeypatch.setattr(app, '_direct_media_requested', lambda: False)
    monkeypatch.setattr(app, 'make_media_url', lambda *a, **k: 'https://signed')
    assert 'gridBlob' not in app._build_photo_summary('lib', 'IMG_1.jpg', _stamped(), include_props=False)


# ---- worker ------------------------------------------------------------------


@pytest.fixture
def blobs(monkeypatch):
    store = {'preview/abc123.jpg': _jpeg((2048, 1536))}
    uploads, stamps = {}, []

    def download(kind, name):
        assert kind == 'thumbnail'
        if name not in store:
            raise ResourceNotFoundError('missing')
        return store[name]

    def update(user_id, filename, updates, *, derive_updates=None, initial_entity=None):
        entity = {'RowKey': filename, 'uploadDate': 'x'}
        stamps.append((user_id, filename, derive_updates(entity) if derive_updates else updates))
        return entity

    monkeypatch.setattr(app, 'download_media_bytes', download)
    monkeypatch.setattr(app, 'upload_media_file', lambda kind, name, data, content_type: uploads.__setitem__(name, (kind, data, content_type)))
    monkeypatch.setattr(app, '_update_metadata_entity_fields', update)
    return SimpleNamespace(store=store, uploads=uploads, stamps=stamps)


def test_make_grid_image_reads_the_preview_and_stamps_the_row(blobs):
    assert app._make_grid_image('lib', _row())
    kind, data, content_type = blobs.uploads['grid/abc123.webp']
    assert (kind, content_type) == ('thumbnail', 'image/webp')
    assert Image.open(io.BytesIO(data)).size == (533, 400)
    [(user_id, filename, updates)] = blobs.stamps
    record = json.loads(updates['gridImage'])
    assert (user_id, filename) == ('lib', 'IMG_1.jpg')
    assert record == {'v': 1, 'blob': 'grid/abc123.webp', 'w': 533, 'h': 400, 'bytes': len(data), 'src': 'preview/abc123.jpg'}
    assert grid_images.served_blob(_row(gridImage=updates['gridImage'])) == 'grid/abc123.webp'


def test_make_grid_image_prefers_the_published_preview_derivative(blobs, monkeypatch):
    monkeypatch.setattr(app, '_derivative_blob_from_metadata', lambda *a, **k: 'derived/v1/s/k/preview/h.jpg')
    blobs.store['derived/v1/s/k/preview/h.jpg'] = _jpeg((1000, 2000))
    assert app._make_grid_image('lib', _row())
    assert json.loads(blobs.stamps[0][2]['gridImage'])['src'] == 'derived/v1/s/k/preview/h.jpg'
    # A receipt whose blob is gone falls back to the preview cache blob.
    blobs.stamps.clear()
    del blobs.store['derived/v1/s/k/preview/h.jpg']
    assert app._make_grid_image('lib', _row())
    assert json.loads(blobs.stamps[0][2]['gridImage'])['src'] == 'preview/abc123.jpg'


def test_make_grid_image_gives_up_without_a_decodable_preview(blobs):
    blobs.store.clear()
    assert not app._make_grid_image('lib', _row())
    blobs.store['preview/abc123.jpg'] = b'garbage'
    assert not app._make_grid_image('lib', _row())
    assert blobs.uploads == {} and blobs.stamps == []


class _Queue:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, body, **kwargs):
        if self.fail:
            raise OSError('queue down')
        self.sent.append(json.loads(body))


def test_grid_job_scans_with_server_side_filters_and_queues_its_continuation(monkeypatch):
    queue = _Queue()
    scans = []
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'metadata_table_client', object())
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda user_id, **kw: scans.append((user_id, kw)) or iter(_rows(3)))
    monkeypatch.setattr(app, '_make_grid_image', lambda user_id, row: True)
    monkeypatch.setattr(grid_images, 'BATCH_SIZE', 2)
    payload = json.loads(grid_images.job_message('lib', cursor="O'Brien.jpg", since='2026-11-11T10:00:00Z', reason='wave'))
    app._handle_clustering_queue_payload(payload, payload['jobId'], 'lib', payload['type'])
    [(user_id, kwargs)] = scans
    assert user_id == 'lib'
    assert kwargs['extra_filter'] == ("preview_status eq 'done' and RowKey gt 'O''Brien.jpg' "
                                      "and Timestamp ge datetime'2026-11-11T10:00:00Z'")
    assert set(kwargs['select']) >= {'gridImage', 'previewArtifact', 'anonymousImageId'}
    [continuation] = queue.sent
    assert (continuation['type'], continuation['user_id'], continuation['cursor'], continuation['since'], continuation['reason']) == (
        'grid_images', 'lib', 'IMG_001.jpg', '2026-11-11T10:00:00Z', 'wave')


def test_grid_job_is_redelivered_when_its_continuation_cannot_be_queued(monkeypatch):
    monkeypatch.setattr(app, 'clustering_queue_client', _Queue(fail=True))
    monkeypatch.setattr(app, 'metadata_table_client', object())
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda user_id, **kw: iter(_rows(3)))
    monkeypatch.setattr(app, '_make_grid_image', lambda user_id, row: True)
    monkeypatch.setattr(grid_images, 'BATCH_SIZE', 2)
    with pytest.raises(RuntimeError):
        app._run_grid_image_job({'user_id': 'lib'})


def test_grid_job_finishing_the_library_queues_nothing(monkeypatch):
    queue = _Queue()
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'metadata_table_client', object())
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda user_id, **kw: iter(_rows(3)))
    monkeypatch.setattr(app, '_make_grid_image', lambda user_id, row: True)
    app._run_grid_image_job({'user_id': 'lib', 'since': 'not-a-date'})
    assert queue.sent == []


def test_grid_jobs_are_off_when_disabled(monkeypatch):
    queue = _Queue()
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setenv('GRID_IMAGES_ENABLED', 'false')
    assert not app._enqueue_grid_images('lib')
    assert queue.sent == []


# ---- triggers ----------------------------------------------------------------


def test_completed_wave_queues_grid_images_for_rows_changed_since_it_opened(monkeypatch):
    import wave_runtime
    calls = []
    wave = {'state': 'complete', 'completionReceiptId': 'r1', 'createdAt': 1_800_000_000.0}
    store = SimpleNamespace(_required=lambda key: wave, wave_key=lambda wave_id: wave_id,
                            pending_handoffs=lambda current: ['index_maintenance'] if current.get('pending', True) else [],
                            complete_handoff=lambda wave_id, name: dict(wave, pending=False))
    monkeypatch.setattr(wave_runtime, 'resume_completed_wave_maintenance', lambda library: None)
    monkeypatch.setattr(app, '_enqueue_grid_images', lambda library, **kw: calls.append((library, kw)))
    assert wave_runtime.execute_wave_handoffs(None, store, 'lib', 'w1', None) == 'done'
    assert calls == [('lib', {'since': '2027-01-15T07:55:00Z', 'reason': 'wave'})]


def test_hard_delete_removes_grid_blobs_with_the_photo(monkeypatch):
    deleted = []
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(
        get_blob_client=lambda container, blob: SimpleNamespace(delete_blob=lambda: deleted.append((container, blob)))))
    assert app._delete_photo_blobs_if_present('abc123') == []
    assert (app.BLOB_THUMBNAIL_CONTAINER, 'grid/abc123.webp') in deleted


# ---- admin action --------------------------------------------------------------


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_library_owner_id', lambda *a, **k: ('lib', None))
    monkeypatch.setattr(app, '_library_cut_over_to_waves', lambda user_id: False)
    queued = []
    monkeypatch.setattr(app, '_enqueue_grid_images', lambda user_id, **kw: queued.append((user_id, kw)) or True)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return SimpleNamespace(client=app.app.test_client(), queued=queued)


def test_admin_grid_backfill_requires_confirmation(http):
    response = http.client.post('/api/admin/backfill/grid-images', json={'repair': True})
    assert response.status_code == 403 and http.queued == []


def test_admin_grid_backfill_queues_the_library(http):
    response = http.client.post('/api/admin/backfill/grid-images', json={'repair': True, 'confirm': 'BACKFILL_GRID_IMAGES'})
    assert response.status_code == 202
    assert response.get_json()['queued'] is True
    assert http.queued == [('lib', {'reason': 'admin_backfill'})]


def test_admin_grid_backfill_waits_for_open_uploads(http, monkeypatch):
    import routes.admin as admin
    monkeypatch.setattr(admin, '_uploads_open', lambda user_id: True)
    body = {'repair': True, 'confirm': 'BACKFILL_GRID_IMAGES'}
    response = http.client.post('/api/admin/backfill/grid-images', json=body)
    assert response.status_code == 409 and response.get_json()['code'] == 'upload_in_progress'
    assert http.queued == []
    assert http.client.post('/api/admin/backfill/grid-images', json=dict(body, force=True)).status_code == 202
