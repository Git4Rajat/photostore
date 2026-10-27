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
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

import ipwork_ocr


class _WordIterator:
    def __init__(self, entries):
        self.entries = entries
        self.index = 0

    def Empty(self, level):
        return not self.entries or self.entries[self.index] is None

    def GetUTF8Text(self, level):
        assert not self.Empty(level), 'Empty layout entry read as a word'
        return self.entries[self.index][0]

    def Confidence(self, level):
        return self.entries[self.index][1]

    def Next(self, level):
        self.index += 1
        return self.index < len(self.entries)


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
            return True

        def GetIterator(self):
            assert received.get('recognized'), 'Iterator read before Recognize()'
            return _WordIterator([('TPLINE', 92.0)])

    class FakeTesserocr:
        RIL = SimpleNamespace(WORD=3)
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
            return True

        def GetIterator(self):
            return _WordIterator([('REAL', 95.0), ('noise', 12.0), ('WORD', 41.0), ('junk', -1.0)])

    class FakeTesserocr:
        RIL = SimpleNamespace(WORD=3)
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
            return True

        def GetIterator(self):
            return _WordIterator([('noise', 10.0), ('junk', -1.0)])

    class FakeTesserocr:
        RIL = SimpleNamespace(WORD=3)
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
            return True

        def GetIterator(self):
            calls.append('GetIterator')
            return _WordIterator([('REAL', 95.0)])

    class FakeTesserocr:
        RIL = SimpleNamespace(WORD=3)
        @staticmethod
        def PyTessBaseAPI(path=None):
            return FakeApi()

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', FakeTesserocr())
    monkeypatch.setattr(ipwork_ocr, '_thread_local', __import__('threading').local())

    ipwork_ocr.process_ocr('owner', 'photo.jpg', jpeg_bytes)

    assert calls == ['SetImage', 'Recognize', 'GetIterator']


def test_process_ocr_times_out_a_hung_recognize_without_closing_the_image(monkeypatch):
    """Confirmed live (2026-10-19): one image hung Recognize() inside
    libtesseract's C++ core on 4 independent attempts, each only resolved by
    the outer ipworker task watchdog killing the whole worker process --
    redelivering the same poison message forever and blocking the entire
    queue behind it. This is the fix: process_ocr must give up on its own,
    well under that outer ceiling, and must NOT close the image out from
    under the still-running (abandoned) worker thread."""
    jpeg_bytes = _make_jpeg_bytes()
    monkeypatch.setattr(ipwork_ocr, 'OCR_CALL_TIMEOUT_SECONDS', 0.05)
    release = threading.Event()
    closed = threading.Event()

    class HangingApi:
        def SetImage(self, image):
            pass

        def Recognize(self):
            release.wait(timeout=5)  # Simulates the real hang; bounded so the test can't actually wedge.
            return True

        def GetIterator(self):
            return _WordIterator([('REAL', 95.0)])

    monkeypatch.setattr(ipwork_ocr, 'tesserocr', SimpleNamespace(RIL=SimpleNamespace(WORD=3)))
    monkeypatch.setattr(ipwork_ocr, '_get_api', lambda: HangingApi())

    real_context = ipwork_ocr.rgb_image

    def tracking_context(*args, **kwargs):
        cm = real_context(*args, **kwargs)
        image = cm.__enter__()
        original_close = image.close
        def tracked_close():
            closed.set()
            original_close()
        image.close = tracked_close
        class _Ctx:
            def __enter__(self):
                return image
            def __exit__(self, *exc):
                return cm.__exit__(*exc)
        return _Ctx()

    monkeypatch.setattr(ipwork_ocr, 'rgb_image', tracking_context)

    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', jpeg_bytes)

    assert result == {'hasData': False, 'error': 'OCR recognition exceeded 0.05s', 'errorCode': 'ocr_timeout'}
    assert not closed.is_set(), 'image must not be closed while the abandoned worker thread may still reference it'
    release.set()  # Let the simulated hang resolve so the background thread doesn't outlive the test.


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


def test_unavailable_engine_reports_bounded_category(monkeypatch):
    monkeypatch.setattr(ipwork_ocr, 'tesserocr', None)
    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', _make_jpeg_bytes())
    assert result['hasData'] is False
    assert result['errorCode'] == 'tesserocr_unavailable'


def test_decode_failure_is_distinct_from_engine_failure(monkeypatch):
    monkeypatch.setattr(ipwork_ocr, 'tesserocr', object())
    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', b'not an image')
    assert result['hasData'] is False
    assert result['errorCode'] == 'ocr_decode_failed'
    def fail():
        raise RuntimeError('engine initialization failed')
    monkeypatch.setattr(ipwork_ocr, '_get_api', fail)
    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', _make_jpeg_bytes())
    assert result['hasData'] is False
    assert result['errorCode'] == 'ocr_engine_failed'


@pytest.mark.parametrize('entries,expected', [([], None), ([None, None], None),
    ([None, ('REAL', 95), None, ('WORD', 90), None], 'REAL WORD')])
def test_empty_layout_entries_are_skipped_without_reading_text(monkeypatch, entries, expected):
    api = SimpleNamespace(SetImage=lambda _: None, Recognize=lambda: True,
                          GetIterator=lambda: _WordIterator(entries))
    monkeypatch.setattr(ipwork_ocr, 'tesserocr', SimpleNamespace(RIL=SimpleNamespace(WORD=3)))
    monkeypatch.setattr(ipwork_ocr, '_get_api', lambda: api)
    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', _make_jpeg_bytes())
    assert result == ({'hasData': True, 'text': expected} if expected else {'hasData': False})


@pytest.mark.parametrize('fault', ['recognition', 'iterator', 'text', 'text-empty-message', 'confidence', 'next'])
def test_real_engine_and_iterator_failures_are_not_collapsed_into_no_data(monkeypatch, fault):
    iterator = _WordIterator([('REAL', 95)])
    def fail(*_):
        raise RuntimeError('Engine failure')
    if fault == 'text-empty-message':
        def fail_text(*_):
            raise RuntimeError('No text returned')
        monkeypatch.setattr(iterator, 'GetUTF8Text', fail_text)
    for name, kind in [('GetUTF8Text', 'text'), ('Confidence', 'confidence'), ('Next', 'next')]:
        if fault == kind:
            monkeypatch.setattr(iterator, name, fail)
    api = SimpleNamespace(SetImage=lambda _: None, Recognize=lambda: fault != 'recognition',
                          GetIterator=lambda: None if fault == 'iterator' else iterator)
    monkeypatch.setattr(ipwork_ocr, 'tesserocr', SimpleNamespace(RIL=SimpleNamespace(WORD=3)))
    monkeypatch.setattr(ipwork_ocr, '_get_api', lambda: api)
    result = ipwork_ocr.process_ocr('owner', 'photo.jpg', _make_jpeg_bytes())
    assert result['hasData'] is False
    assert result['errorCode'] == 'ocr_engine_failed'
    assert result['error']


def test_real_engine_handles_text_free_images_and_reuse_without_losing_text(monkeypatch):
    from PIL import ImageDraw, ImageFont
    if ipwork_ocr.tesserocr is None:
        pytest.skip('tesserocr not installed')
    candidates = [ipwork_ocr._TESSDATA_PATH, '/opt/homebrew/share/tessdata']
    tessdata = next((p for p in candidates if p and (Path(p) / 'eng.traineddata').is_file()), None)
    if not tessdata:
        pytest.skip('English Tesseract data not installed')
    monkeypatch.setattr(ipwork_ocr, '_TESSDATA_PATH', tessdata)
    local = threading.local()
    monkeypatch.setattr(ipwork_ocr, '_thread_local', local)
    small = Image.new('RGB', (200, 50), 'white')
    ImageDraw.Draw(small).text((5, 15), 'PHOTO STORE 2026', fill='black', font=ImageFont.load_default())
    text = small.resize((800, 200))
    outputs = []
    try:
        for image in [Image.new('RGB', (800, 200), 'white'), text, Image.new('RGB', (800, 200), 'white'), text]:
            stream = io.BytesIO(); image.save(stream, 'JPEG')
            outputs.append(ipwork_ocr.process_ocr('owner', 'photo.jpg', stream.getvalue()))
        assert outputs[0] == outputs[2] == {'hasData': False}
        assert outputs[1]['hasData'] is True and 'PHOTO' in outputs[1]['text']
        assert outputs[1] == outputs[3]
    finally:
        if getattr(local, 'api', None) is not None:
            local.api.End()


def test_ocr_subthread_attributes_engine_initialization_and_recognition(monkeypatch):
    import processing_metrics as metrics
    class Api:
        def SetImage(self, image): pass
        def Recognize(self): return True
        def GetIterator(self): return _WordIterator([('WORD', 95.0)])
    monkeypatch.setattr(ipwork_ocr, 'tesserocr', SimpleNamespace(
        RIL=SimpleNamespace(WORD=3), PyTessBaseAPI=lambda **kwargs: Api()))
    monkeypatch.setattr(ipwork_ocr, '_thread_local', threading.local())
    with metrics.accounting() as wave, metrics.accounting() as image:
        assert ipwork_ocr.process_ocr('owner', 'photo.jpg', _make_jpeg_bytes())['hasData']
    for counter in (wave, image):
        values = counter.snapshot()
        assert values['ocr_engine_inits'] == 1
        assert values['ocr_engine_init_ms'] >= 0
        assert values['ocr_recognition_ms'] >= 0
    # OCR's raw thread must not leak its context into later calls.
    assert wave.snapshot() == image.snapshot()
