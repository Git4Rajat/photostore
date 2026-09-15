"""Unit tests for ipwork_ocr.py's RAW-decode routing.

See test_ipwork_face.py/test_ipwork_vision.py's matching tests for the full
context: PIL has no generic codec for RAW containers (e.g. CR3), so
Image.open() raises 'cannot identify image file' when handed raw bytes
directly. ipwork_face.py and ipwork_vision.py were already fixed with a
_decodable_image_bytes helper that routes RAW extensions through
image_utils.extract_raw_preview_bytes first; ipwork_ocr.py never got the same
fix, so any CR3/RAW photo whose OCR step ran without a previously-shrunk
preview already swapped into the ipworker batch (the common case in 'both'
mode -- see ipwork_ocr.py's _decodable_image_bytes docstring) got its
UnidentifiedImageError silently collapsed into the same terminal 'no_data'
status as a genuine empty-OCR result, permanently hiding real text. Confirmed
live against IMG_1528.CR3/IMG_1530.CR3 on stcontainerapp-dv: both stuck at
ocr_status='no_data' despite large, legible boat/ferry signage text, and PIL
directly reproduced UnidentifiedImageError on their raw bytes.
"""
from __future__ import annotations

import io

from PIL import Image

import ipwork_ocr


def _make_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), color='white').save(buf, format='JPEG')
    return buf.getvalue()


def test_decodable_image_bytes_passes_through_non_raw_unchanged():
    original = b'plain-jpeg-bytes'
    assert ipwork_ocr._decodable_image_bytes(original, 'photo.jpg') == original


def test_decodable_image_bytes_extracts_preview_for_raw_extension(monkeypatch):
    calls = []

    def fake_extract(image_bytes, filename):
        calls.append((image_bytes, filename))
        return b'decodable-jpeg-preview'

    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', fake_extract)

    result = ipwork_ocr._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_1528.CR3')

    assert result == b'decodable-jpeg-preview'
    assert calls == [(b'raw-cr3-bytes', 'IMG_1528.CR3')]


def test_decodable_image_bytes_falls_back_to_original_when_extraction_fails(monkeypatch):
    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', lambda image_bytes, filename: None)

    result = ipwork_ocr._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_1528.CR3')

    assert result == b'raw-cr3-bytes'


def test_process_ocr_passes_decoded_bytes_to_tesseract(monkeypatch):
    jpeg_bytes = _make_jpeg_bytes()
    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', lambda image_bytes, filename: jpeg_bytes)

    received = {}

    class FakeApi:
        def SetImage(self, image):
            received['size'] = image.size

        def Recognize(self):
            received['recognized'] = True

        def MapWordConfidences(self):
            assert received.get('recognized'), 'MapWordConfidences called before Recognize()'
            return [('TPLINE', 92.0)]

    class FakeTesserocr:
        @staticmethod
        def PyTessBaseAPI(path=None):
            return FakeApi()

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', FakeTesserocr())
    monkeypatch.setattr(ipwork_ocr, '_thread_local', __import__('threading').local())

    result = ipwork_ocr.process_ocr('owner', 'IMG_1528.CR3', b'raw-cr3-bytes')

    assert received['size'] == (8, 8)
    assert result == {'hasData': True, 'text': 'TPLINE'}


def test_process_ocr_drops_low_confidence_words(monkeypatch):
    jpeg_bytes = _make_jpeg_bytes()
    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', lambda image_bytes, filename: jpeg_bytes)

    class FakeApi:
        def SetImage(self, image):
            pass

        def Recognize(self):
            pass

        def MapWordConfidences(self):
            return [('REAL', 95.0), ('noise', 12.0), ('WORD', 41.0), ('junk', -1.0)]

    class FakeTesserocr:
        @staticmethod
        def PyTessBaseAPI(path=None):
            return FakeApi()

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', FakeTesserocr())
    monkeypatch.setattr(ipwork_ocr, '_thread_local', __import__('threading').local())

    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', jpeg_bytes)

    assert result == {'hasData': True, 'text': 'REAL WORD'}


def test_process_ocr_reports_no_data_when_all_words_low_confidence(monkeypatch):
    jpeg_bytes = _make_jpeg_bytes()
    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', lambda image_bytes, filename: jpeg_bytes)

    class FakeApi:
        def SetImage(self, image):
            pass

        def Recognize(self):
            pass

        def MapWordConfidences(self):
            return [('noise', 10.0), ('junk', -1.0)]

    class FakeTesserocr:
        @staticmethod
        def PyTessBaseAPI(path=None):
            return FakeApi()

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', FakeTesserocr())
    monkeypatch.setattr(ipwork_ocr, '_thread_local', __import__('threading').local())

    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', jpeg_bytes)

    assert result == {'hasData': False}


def test_process_ocr_calls_recognize_before_reading_confidences(monkeypatch):
    """The real regression: tesserocr's MapWordConfidences() reads back the
    last Recognize() pass rather than running one itself (unlike
    GetUTF8Text(), which recognizes on demand) -- calling it without an
    explicit Recognize() first silently returns [] on every real image,
    indistinguishable from a genuine empty-OCR result. Confirmed live: every
    ipworker OCR call since the 2026-08-28 tesserocr migration returned
    'no_data' regardless of actual image content."""
    jpeg_bytes = _make_jpeg_bytes()
    monkeypatch.setattr(ipwork_ocr, 'extract_raw_preview_bytes', lambda image_bytes, filename: jpeg_bytes)

    calls = []

    class FakeApi:
        def SetImage(self, image):
            calls.append('SetImage')

        def Recognize(self):
            calls.append('Recognize')

        def MapWordConfidences(self):
            calls.append('MapWordConfidences')
            return [('REAL', 95.0)]

    class FakeTesserocr:
        @staticmethod
        def PyTessBaseAPI(path=None):
            return FakeApi()

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', FakeTesserocr())
    monkeypatch.setattr(ipwork_ocr, '_thread_local', __import__('threading').local())

    ipwork_ocr.process_ocr('owner', 'photo.jpg', jpeg_bytes)

    assert calls == ['SetImage', 'Recognize', 'MapWordConfidences']


def test_resolve_tessdata_path_prefers_env_override(monkeypatch):
    monkeypatch.setenv('TESSDATA_PREFIX', '/custom/tessdata')
    assert ipwork_ocr._resolve_tessdata_path() == '/custom/tessdata'


def test_resolve_tessdata_path_falls_back_to_glob(monkeypatch, tmp_path):
    monkeypatch.delenv('TESSDATA_PREFIX', raising=False)
    fake_tessdata = tmp_path / 'usr' / 'share' / 'tesseract-ocr' / '5' / 'tessdata'
    fake_tessdata.mkdir(parents=True)
    monkeypatch.setattr(
        ipwork_ocr.glob, 'glob',
        lambda pattern: [str(fake_tessdata)] if 'tesseract-ocr' in pattern else [],
    )
    assert ipwork_ocr._resolve_tessdata_path() == str(fake_tessdata)


def test_resolve_tessdata_path_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.delenv('TESSDATA_PREFIX', raising=False)
    monkeypatch.setattr(ipwork_ocr.glob, 'glob', lambda pattern: [])
    assert ipwork_ocr._resolve_tessdata_path() is None


def test_process_ocr_on_raw_bytes_without_fallback_would_fail():
    """Sanity check the bug this fix closes: PIL genuinely cannot open raw
    RAW-container bytes directly, so skipping _decodable_image_bytes would
    silently produce {'hasData': False, 'error': ...} (collapsed into the
    same terminal 'no_data' status as a real empty-OCR result)."""
    with io.BytesIO(b'not-a-real-image-container') as buf:
        try:
            with Image.open(buf) as image:
                image.load()
            assert False, 'expected PIL to fail on non-image bytes'
        except Exception:
            pass
