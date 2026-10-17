"""Backend contract for authoritative in-browser ExifTool verdicts.

When the browser runs the SAME ExifTool (pinned version) on the full file, its
JSON is authoritative: the backend normalizes it with its own shared normalizer
(identical stored fields) and NEVER fetches the original -- re-running the same
tool server-side would be identical. The server fallback still runs only for
clients that could not run the browser tool (no browser_exiftool result present).
"""
from __future__ import annotations

import json

import pytest

import storage_utils


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(storage_utils, 'mark_step_done', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'mark_step_no_data', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_reverse_geocode_fallback',
                        lambda lat, lon: {'city': 'Townsville', 'country': 'US', 'region': 'CA'})
    monkeypatch.setattr(storage_utils, '_build_client_semantic_text', lambda *a, **k: '')
    monkeypatch.setattr(storage_utils, 'build_semantic_layers', lambda *a, **k: [])
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)


def _md():
    return {'PartitionKey': 'u', 'RowKey': 'p.nef', 'processing_metadata': '{}'}


def _authoritative(obj, **extra):
    return {'source': 'browser_exiftool', 'verifiedComplete': True, 'exifToolJson': obj, **extra}


def _client_exif(md):
    return json.loads(md['processing_metadata'])['client_exif']


def test_authoritative_with_data_is_done_and_geocoded(ctx):
    md = _md()
    obj = {'IFD0:Make': 'NIKON', 'IFD0:Model': 'Z 6',
           'ExifIFD:DateTimeOriginal': '2020:05:06 07:08:09',
           'EXIF:GPSLatitude': '37.5', 'EXIF:GPSLatitudeRef': 'N',
           'EXIF:GPSLongitude': '122.1', 'EXIF:GPSLongitudeRef': 'W'}
    su = storage_utils._apply_browser_exiftool_result('u', 'p.nef', md, _authoritative(obj), 'b:1', apply_geo=True)
    assert su['exif_status'] == 'done'
    assert su['map_detection_status'] == 'done'
    cf = _client_exif(md)
    assert cf['source'] == 'browser_exiftool' and cf['hasGps'] is True
    assert md.get('locationCity') == 'Townsville'


def test_authoritative_no_exif_is_verified_no_data(ctx):
    md = _md()
    su = storage_utils._apply_browser_exiftool_result(
        'u', 'p.jpg', md, _authoritative({'SourceFile': 'x', 'File:FileName': 'p.jpg'}), 'b:1', apply_geo=True)
    assert su['exif_status'] == 'no_data'
    assert su['map_detection_status'] == 'no_data'
    assert _client_exif(md)['verifiedNoExif'] is True


@pytest.mark.parametrize('error_value', ['Unknown file type', 'Unsupported RAW file type', 'Unsupported file type'])
def test_authoritative_unsupported_format_is_surfaced(ctx, error_value):
    md = _md()
    su = storage_utils._apply_browser_exiftool_result(
        'u', 'p.lri', md, _authoritative({'File:Error': error_value}), 'b:1', apply_geo=True)
    assert su['exif_status'] == 'unsupported'
    cf = _client_exif(md)
    assert cf.get('unsupportedFormat') is True
    assert cf.get('errorCode') == 'exif_unsupported_format'
    # No GPS possible from an unparseable file.
    assert su['map_detection_status'] == 'no_data'


def test_authoritative_parser_error_is_failed_terminal(ctx):
    md = _md()
    su = storage_utils._apply_browser_exiftool_result(
        'u', 'p.nef', md, _authoritative({'File:Error': 'some genuine parse problem'}), 'b:1', apply_geo=True)
    assert su['exif_status'] == 'failed'
    assert su['map_detection_status'] == 'failed'


def test_handler_takes_no_image_getter_so_cannot_fetch_original():
    # Structural guarantee: the handler's only input is the browser result; it
    # has no way to download the original.
    import inspect
    params = list(inspect.signature(storage_utils._apply_browser_exiftool_result).parameters)
    assert 'get_image_bytes' not in params and 'image_bytes' not in params


# ---- gate: the apply block routes authoritative results here (no fetch) and
#      leaves the server fallback for non-authoritative/tool-unavailable clients ----

@pytest.fixture
def apply_ctx(ctx, monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_extract_and_store_embeddings', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)


def test_apply_gate_authoritative_exif_never_fetches_original(apply_ctx):
    md = _md()
    fetched = []

    def getter():
        fetched.append(1)
        raise AssertionError('authoritative EXIF must not fetch the original')

    storage_utils._apply_client_processing_results(
        'u', 'p.nef', md, getter,
        {'exif': _authoritative({'IFD0:Make': 'NIKON', 'ExifIFD:DateTimeOriginal': '2020:05:06 07:08:09'})},
        [], 'browser:1', origin='browser', persist_result=False)
    assert fetched == []
    assert md.get('exif_status') == 'done'


def test_apply_gate_non_authoritative_empty_still_falls_back(apply_ctx, monkeypatch):
    # Today's clients (no browser_exiftool result) keep the existing fallback,
    # which DOES read the original -- proving the change is additive.
    md = _md()
    fetched = []
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing',
                        lambda data, name: fetched.append(1) or {})

    storage_utils._apply_client_processing_results(
        'u', 'p.nef', md, lambda: b'original-bytes',
        {'exif': {'hasData': False, 'source': 'browser'}},
        [], 'browser:1', origin='browser', persist_result=False)
    assert fetched == [1]  # server fallback read the original
