"""Unit tests for the first-result-wins race guard added for ipworker.

In 'both' processing mode the browser and a server-side ipworker can both
compute a result for the same photo/step. _apply_client_processing_results was
previously last-write-wins for ocr/map_detection/face/ai_vision/thumbnail,
which is fine with a single writer but would let the two race and clobber
each other. These tests confirm _step_locked_done (and, for thumbnail,
had_thumbnail_before) blocks a second write once a step is already 'done',
while a legitimate first write for a not-yet-done step still lands.
"""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])


def _tiny_jpeg_bytes() -> bytes:
    image = Image.new('RGB', (32, 32), color=(10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG')
    return buffer.getvalue()


def _seed_row(metadata: _FakeMetadataTable, user_id: str, filename: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'processing_metadata': '{}',
        **overrides,
    }
    metadata.upsert_entity(row)


class _Calls:
    def __init__(self) -> None:
        self.face_store_calls: list = []


@pytest.fixture
def processing_ctx(monkeypatch):
    metadata = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda kind, name: _tiny_jpeg_bytes())
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'refresh_user_vector_index', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a, **k: None)
    calls = _Calls()
    monkeypatch.setattr(
        storage_utils, '_store_client_face_entities',
        lambda user_id, filename, faces, **k: calls.face_store_calls.append((user_id, filename, faces)) or [],
    )
    yield metadata, calls


# --- _step_locked_done: pure function ---------------------------------------

def test_step_locked_done_true_when_done():
    assert storage_utils._step_locked_done({'ocr_status': 'done'}, 'ocr') is True


@pytest.mark.parametrize('status', ['pending', 'queued', 'running', 'failed', 'no_data', ''])
def test_step_locked_done_false_for_other_statuses(status):
    assert storage_utils._step_locked_done({'ocr_status': status}, 'ocr') is False


def test_step_locked_done_false_when_missing():
    assert storage_utils._step_locked_done({}, 'ocr') is False


# --- exif ------------------------------------------------------------------

def test_exif_write_applies_when_not_yet_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, exif_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'exif': {
            'hasData': True,
            'data': {},
            'latitude': 40.0,
            'longitude': -73.0,
        }},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
        origin='browser',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == 'done'
    assert float(stored['latitude']) == 40.0


def test_exif_second_writer_discarded_once_done(processing_ctx):
    """Simulates 'both' mode: ipworker's EXIF result loses the race because
    the browser's already landed and marked exif done. The browser's hand-
    rolled parser and the server's exiftool-based one can disagree at the
    margins, so a second write must not overwrite already-accepted GPS."""
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(
        metadata, user_id, filename,
        exif_status='done', latitude=40.0, longitude=-73.0,
    )

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'exif': {
            'hasData': True,
            'data': {},
            'latitude': 10.0,
            'longitude': 20.0,
        }},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == 'done'
    assert float(stored['latitude']) == 40.0
    assert stored['longitude'] == -73.0


# --- thumbnail -----------------------------------------------------------------

def test_thumbnail_write_applies_when_not_yet_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, thumbnail_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'thumbnail': {
            'hasData': True,
            'contentType': 'image/jpeg',
            'data': base64.b64encode(b'first-thumb-bytes').decode('ascii'),
        }},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['thumbnail_status'] == 'done'


def test_thumbnail_second_writer_discarded_once_done(processing_ctx):
    """Simulates 'both' mode: ipworker's thumbnail loses the race because the
    browser's kickOffThumbnailForFile already landed and marked it done."""
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(
        metadata, user_id, filename,
        thumbnail_status='done',
        processing_metadata=json.dumps({'client_thumbnail': {'source': 'browser'}}),
    )

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'thumbnail': {
            'hasData': True,
            'contentType': 'image/jpeg',
            'data': base64.b64encode(b'second-thumb-bytes-should-be-discarded').decode('ascii'),
        }},
        client_processing_report=[],
        client_asset_id='ipworker:job-2',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['thumbnail_status'] == 'done'
    processing = json.loads(stored['processing_metadata'])
    assert processing['client_thumbnail']['source'] == 'browser'


def test_thumbnail_hasdata_false_resolves_via_server_fallback_not_stuck_running(processing_ctx):
    """ipworker reports failure inline via clientProcessing.thumbnail's hasData
    (it never populates the separate clientProcessingReport array the
    browser's failure path relies on -- see _run_ipwork_steps in app.py).
    Without the hasData-aware fallback trigger, this left thumbnail_status
    stuck at 'running' forever instead of resolving to a terminal state."""
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, thumbnail_status='running')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'thumbnail': {'hasData': False, 'error': 'decode_failed'}},
        client_processing_report=[],
        client_asset_id='ipworker:job-3',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['thumbnail_status'] == 'done'
    processing = json.loads(stored['processing_metadata'])
    assert processing['server_thumbnail']['fallbackFor'] == 'browser_thumbnail_failed'


# --- ocr ---------------------------------------------------------------------

def test_ocr_write_applies_when_not_yet_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ocr': {'text': 'hello world'}},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
        origin='browser',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['ocr_status'] == 'done'
    assert stored['ocrText'] == 'hello world'


def test_ocr_second_writer_discarded_once_done(processing_ctx):
    """Simulates 'both' mode: ipworker's OCR result loses the race because the
    browser's already landed and marked the step done."""
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(
        metadata, user_id, filename,
        ocr_status='done', ocrText='browser text (won the race)',
    )

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ocr': {'text': 'ipworker text (should be discarded)'}},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['ocr_status'] == 'done'
    assert stored['ocrText'] == 'browser text (won the race)'


# --- map_detection -----------------------------------------------------------

def test_map_detection_write_applies_when_not_yet_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, map_detection_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'map_detection': {
            'latitude': 40.0, 'longitude': -73.0,
            'address': '1 Way', 'city': 'Townsville', 'country': 'Countryland',
        }},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
        origin='browser',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['map_detection_status'] == 'done'
    assert stored['locationCity'] == 'Townsville'


def test_map_detection_second_writer_discarded_once_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(
        metadata, user_id, filename,
        map_detection_status='done', locationCity='FirstCity', locationCountry='FirstCountry',
    )

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'map_detection': {
            'latitude': 10.0, 'longitude': 20.0,
            'address': 'Other', 'city': 'SecondCity', 'country': 'SecondCountry',
        }},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['locationCity'] == 'FirstCity'
    assert stored['locationCountry'] == 'FirstCountry'


# --- ai_vision -----------------------------------------------------------------

def _ai_vision_payload(caption: str) -> dict:
    return {
        'hasData': True,
        'tags': ['dog', 'outdoor'],
        'caption': caption,
        'modelAvailability': 'available',
        'modelVersion': 'v1',
        'modelTaxonomyVersion': 'tax-v1',
        'runtime': 'transformers.js',
    }


def test_ai_vision_write_applies_when_not_yet_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ai_vision_status='pending', tags='[]')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ai_vision': _ai_vision_payload('a dog outside')},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
        origin='browser',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['ai_vision_status'] == 'done'
    assert stored['caption'] == 'a dog outside'


def test_ai_vision_second_writer_discarded_once_done(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(
        metadata, user_id, filename,
        ai_vision_status='done', caption='first caption (won the race)', tags='[]',
    )

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ai_vision': _ai_vision_payload('second caption (should be discarded)')},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['caption'] == 'first caption (won the race)'


def test_ai_vision_failure_without_model_provenance_resolves_to_failed(processing_ctx):
    """Every one of ipwork_vision.py's failure paths (CLIP model unavailable,
    vocabulary missing, embedding failed, an unhandled exception via
    _run_ipwork_steps) omits modelAvailability/modelVersion/etc entirely, so
    model_ready is always False for them -- this used to be a silent no-op on
    ai_vision_status, leaving it stuck at 'running' forever instead of
    resolving to a retryable terminal state."""
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ai_vision_status='running')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ai_vision': {'hasData': False, 'error': 'image_embedding_failed'}},
        client_processing_report=[],
        client_asset_id='ipworker:job-2',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    assert stored['ai_vision_status'] == 'failed'


# --- face ----------------------------------------------------------------------

def test_face_write_applies_when_not_yet_done(processing_ctx):
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, face_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'face': {'faces': []}},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
        origin='browser',
    )

    assert len(calls.face_store_calls) == 1


def test_face_second_writer_skipped_once_done(processing_ctx):
    """Once face detection is done, a second detector's write (e.g. ipworker's,
    running a different landmark/alignment pipeline) must not even attempt to
    merge/overwrite the already-stored faces for this photo."""
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, face_status='done', faceCount=1)

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'face': {'faces': []}},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    assert calls.face_store_calls == []
    assert metadata.get_entity(user_id, filename)['face_status'] == 'done'


# --- origin provenance -----------------------------------------------------------

def test_origin_is_stamped_into_processing_metadata(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ocr': {'text': 'server side text'}},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )

    stored = metadata.get_entity(user_id, filename)
    processing = json.loads(stored['processing_metadata'])
    assert processing['client_ocr']['source'] == 'ipworker'


def test_default_origin_is_browser(processing_ctx):
    metadata, _ = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')

    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'ocr': {'text': 'client side text'}},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
    )

    stored = metadata.get_entity(user_id, filename)
    processing = json.loads(stored['processing_metadata'])
    assert processing['client_ocr']['source'] == 'browser'
