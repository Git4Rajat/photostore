"""Unit tests for app._processing_step_reasons, the compact per-step failure
reason the /photos/processing-status poller now surfaces so a 'failed'/
'no_data' tile explains itself instead of needing a server-side dig."""
import json

import app


def _md(processing, **statuses):
    return {'processing_metadata': json.dumps(processing), **statuses}


def test_surfaces_reason_for_each_non_done_step():
    md = _md(
        {
            'client_face': {'faceFailureStage': 'quality_filter_rejected',
                            'filteredReason': 'landmark_detection_failed'},
            'client_exif': {'errorCode': 'exif_processing_failed'},
            'client_ocr': {'errorCode': 'ocr_decode_failed'},
            'client_ai_vision_rejected': {'reason': 'invalid_or_missing_model_provenance'},
            'client_map_detection': {'errorCode': 'geo_invalid_result'},
            'client_thumbnail_rejected': {'reason': 'byte_cap_exceeded'},
            'client_preview_rejected': {'reason': 'edge_cap_exceeded'},
        },
        face_status='no_data', exif_status='failed', ocr_status='failed',
        ai_vision_status='failed', map_detection_status='failed',
        thumbnail_status='pending', preview_status='pending',
    )
    assert app._processing_step_reasons(md) == {
        'face': 'quality_filter_rejected',
        'exif': 'exif_processing_failed',
        'ocr': 'ocr_decode_failed',
        'aiVision': 'invalid_or_missing_model_provenance',
        'mapDetection': 'geo_invalid_result',
        'thumbnail': 'byte_cap_exceeded',
        'preview': 'edge_cap_exceeded',
    }


def test_done_step_never_surfaces_a_stale_breadcrumb():
    # A later successful pass can leave an old failure breadcrumb in
    # processing_metadata; a 'done' status must not report it as a reason.
    md = _md({'client_face': {'faceFailureStage': 'postprocessing_failed'}}, face_status='done')
    assert app._processing_step_reasons(md) == {}


def test_exif_prefers_client_then_server_errorcode():
    md = _md({'server_exif': {'errorCode': 'exif_storage_limit'}}, exif_status='failed')
    assert app._processing_step_reasons(md) == {'exif': 'exif_storage_limit'}


def test_no_reason_available_is_omitted_even_when_failed():
    # Status is failed but there is no diagnosable breadcrumb -> omitted, not an
    # empty string, so the common case stays lean.
    md = _md({}, face_status='failed', ocr_status='failed')
    assert app._processing_step_reasons(md) == {}


def test_absent_or_malformed_metadata_yields_empty():
    assert app._processing_step_reasons({}) == {}
    assert app._processing_step_reasons({'processing_metadata': 'not json'}) == {}
    assert app._processing_step_reasons({'processing_metadata': '[]'}) == {}
    assert app._processing_step_reasons({'processing_metadata': None}) == {}
