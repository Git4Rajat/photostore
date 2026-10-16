"""Apply builds final semantic inputs once and attributes its remaining work."""
import json
import logging

import pytest

import perf_instrumentation
import processing_diagnostics
import storage_utils
from fakes import FakeTable


@pytest.fixture
def apply_context(monkeypatch):
    metadata, embeddings = FakeTable(), FakeTable()
    metadata.upsert_entity({'PartitionKey': 'private-user', 'RowKey': 'private.jpg',
        'processing_metadata': '{}', 'processing_lease_owner': 'worker',
        'processing_lease': 'worker', 'ocr_status': 'running', 'exif_status': 'running',
        'map_detection_status': 'running', 'ai_vision_status': 'running'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', embeddings)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: pytest.fail('Original read'))
    monkeypatch.setattr(storage_utils, '_reverse_geocode_fallback', lambda *a: {'city': 'Test City', 'country': 'Test Country'})
    encoded = []
    monkeypatch.setattr(storage_utils.vision_utils, 'encode_text_embedding', lambda text: encoded.append(text) or [0.2, 0.8])
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'test-v1')
    return metadata, embeddings, encoded


def apply(payload):
    return storage_utils.apply_client_processing_results_for_file(
        'private-user', 'private.jpg', origin='ipworker', lease_owner='worker',
        client_processing=payload)


def events(caplog):
    prefix = 'processing telemetry '
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix) and 'processing_apply_phase' in record.getMessage()]


@pytest.mark.parametrize('explicit_geo', [False, True])
def test_mixed_results_build_semantic_inputs_once_with_identical_final_values(apply_context, monkeypatch, explicit_geo):
    metadata, embeddings, encoded = apply_context
    calls = {'text': 0, 'layers': 0}
    text_builder = storage_utils._build_client_semantic_text
    layer_builder = storage_utils.build_semantic_layers
    def text(*args):
        calls['text'] += 1
        return text_builder(*args)
    def layers(*args):
        calls['layers'] += 1
        return layer_builder(*args)
    monkeypatch.setattr(storage_utils, '_build_client_semantic_text', text)
    monkeypatch.setattr(storage_utils, 'build_semantic_layers', layers)
    payload = {
        'ocr': {'hasData': True, 'text': 'private document text'},
        'exif': {'hasData': True, 'latitude': 10, 'longitude': 20, 'data': {'Make': 'Test'}},
        'ai_vision': {'hasData': True, 'tags': ['dog', 'outdoor'], 'modelAvailability': 'available',
                      'modelVersion': 'v1', 'modelTaxonomyVersion': 'tax-v1', 'runtime': 'transformers.js'},
    }
    if explicit_geo:
        payload['map_detection'] = {'hasData': True, 'latitude': 10, 'longitude': 20,
                                    'city': 'Test City', 'country': 'Test Country'}
    result = apply(payload)
    assert calls == {'text': 1, 'layers': 1}  # Previously four builds of each.
    assert encoded == [text_builder('private.jpg', result)]
    assert result['semanticText'] == encoded[0]
    assert json.loads(result['semanticLayers']) == layer_builder('private.jpg', result)
    assert result['locationCity'] == 'Test City'
    assert result['ocrText'] == 'private document text'
    assert all(result[f'{step}_status'] == 'done' for step in ('ocr', 'exif', 'map_detection', 'ai_vision'))
    assert json.loads(embeddings.rows[('private-user', 'private.jpg')]['semanticEmbedding']) == [0.2, 0.8]
    assert metadata.rows[('private-user', 'private.jpg')]['processing_lease_owner'] == ''


def test_apply_phases_separate_cpu_storage_and_index_work(apply_context, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='processing_diagnostics')
    table = apply_context[0]
    original = table.update_entity
    def update(*args, **kwargs):
        perf_instrumentation.record_io('table', 'PATCH', '/private-table/private.jpg', 204, 12, 0)
        return original(*args, **kwargs)
    monkeypatch.setattr(table, 'update_entity', update)
    with perf_instrumentation.scope('test'):
        apply({'ocr': {'hasData': True, 'text': 'private document text'}})
    rows = events(caplog)
    assert [row['phase'] for row in rows] == ['semantic_refresh', 'embedding_write', 'metadata_cas', 'index_dirty']
    assert all(row['outcome'] == 'success' and row['milliseconds'] >= 0 and row['thread_cpu_ms'] >= 0 for row in rows)
    assert [row['sdk_calls'] for row in rows] == [0, 0, 1, 0]
    assert rows[2]['sdk_send_ms'] == 12
    assert all(secret not in json.dumps(rows) for secret in ('private-user', 'private.jpg', 'private document text'))


def test_apply_phase_failure_is_attributed_and_does_not_hide_business_error(apply_context, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='processing_diagnostics')
    def fail(*args):
        raise RuntimeError('private model failure')
    monkeypatch.setattr(storage_utils.vision_utils, 'encode_text_embedding', fail)
    with pytest.raises(RuntimeError, match='private model failure'):
        apply({'ocr': {'hasData': True, 'text': 'private document text'}})
    rows = events(caplog)
    assert rows[0]['phase'] == 'semantic_refresh' and rows[0]['outcome'] == 'error'
    assert 'private model failure' not in json.dumps(rows)
    assert apply_context[0].rows[('private-user', 'private.jpg')]['processing_lease_owner'] == ''


def test_face_persistence_has_its_own_phase(apply_context, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='processing_diagnostics')
    def store(*args, **kwargs):
        perf_instrumentation.record_io('table', 'PUT', '/face-lookup/private.jpg', 204, 8, 0)
        return []
    monkeypatch.setattr(storage_utils, '_store_client_face_entities', store)
    with perf_instrumentation.scope('test'):
        apply({'face': {'hasData': False, 'faces': [], 'rawFaceCount': 0, 'faceModelReady': True}})
    row = next(row for row in events(caplog) if row['phase'] == 'face_persistence')
    assert row['sdk_calls'] == 1 and row['sdk_send_ms'] == 8


def test_phase_survives_broken_io_diagnostics_and_resets_on_exception(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='processing_diagnostics')
    def fail():
        raise RuntimeError('telemetry unavailable')
    monkeypatch.setattr(perf_instrumentation, 'current_scope', fail)
    with pytest.raises(ValueError, match='business error'):
        with processing_diagnostics.apply_phase('private-user', 'private.jpg', 'metadata_cas'):
            raise ValueError('business error')
    row, = events(caplog)
    assert row['outcome'] == 'error' and 'sdk_calls' not in row
