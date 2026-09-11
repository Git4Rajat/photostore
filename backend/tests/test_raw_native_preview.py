"""Regression tests for the RAW "FR" (full resolution) native-preview path.

extract_raw_native_preview_from_path/_bytes back the
/api/photos/raw-full-preview/<filename> route used by the lightbox's FR
button. They must only ever return an *existing* embedded preview at its
native size -- never fall back to rawpy's raw.postprocess() full demosaic,
which has no timeout or resource guard anywhere in this codebase and is
only safe today because it's confined to the async preview-generation
worker job, not a synchronous web request.
"""
from __future__ import annotations

import io
import sys
import types

from PIL import Image

import image_utils as iu


def _solid_jpeg(width: int, height: int) -> bytes:
    image = Image.new('RGB', (width, height), (10, 20, 30))
    buf = io.BytesIO()
    image.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


def test_extract_raw_native_preview_from_path_picks_largest_candidate(monkeypatch):
    small = _solid_jpeg(100, 80)
    large = _solid_jpeg(3000, 2000)
    monkeypatch.setattr(iu, '_extract_exiftool_preview_from_path', lambda path, flip: small)
    monkeypatch.setattr(iu, 'extract_embedded_jpeg_from_path', lambda path, flip: large)
    monkeypatch.setattr(iu, '_extract_rawpy_thumb_only_from_path', lambda path: None)

    result = iu.extract_raw_native_preview_from_path('/fake/path.cr3')

    assert result == large


def test_extract_raw_native_preview_from_path_returns_none_without_any_candidate(monkeypatch):
    monkeypatch.setattr(iu, '_extract_exiftool_preview_from_path', lambda path, flip: None)
    monkeypatch.setattr(iu, 'extract_embedded_jpeg_from_path', lambda path, flip: None)
    monkeypatch.setattr(iu, '_extract_rawpy_thumb_only_from_path', lambda path: None)

    assert iu.extract_raw_native_preview_from_path('/fake/path.cr3') is None


def test_extract_raw_native_preview_from_path_does_not_shrink_large_preview(monkeypatch):
    # A 3000px-wide embedded preview is well over PREVIEW_MAX_DIMENSION (2048) --
    # the native-preview path must return it untouched, unlike the default
    # shrunk-preview path which funnels everything through _encode_preview_jpeg.
    large = _solid_jpeg(3000, 2000)
    monkeypatch.setattr(iu, '_extract_exiftool_preview_from_path', lambda path, flip: large)
    monkeypatch.setattr(iu, 'extract_embedded_jpeg_from_path', lambda path, flip: None)
    monkeypatch.setattr(iu, '_extract_rawpy_thumb_only_from_path', lambda path: None)

    result = iu.extract_raw_native_preview_from_path('/fake/path.cr3')

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (3000, 2000)


def _install_fake_rawpy(monkeypatch, postprocess_calls, *, extract_thumb_raises: bool):
    class FakeThumbFormat:
        JPEG = 'jpeg'
        BITMAP = 'bitmap'

    class FakeRaw:
        sizes = types.SimpleNamespace(flip=0)

        def extract_thumb(self):
            if extract_thumb_raises:
                raise RuntimeError('no embedded thumbnail in this RAW file')
            raise AssertionError('test did not expect extract_thumb to succeed')

        def postprocess(self, *args, **kwargs):
            postprocess_calls.append(True)
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_rawpy = types.SimpleNamespace(ThumbFormat=FakeThumbFormat, imread=lambda *_: FakeRaw())
    monkeypatch.setitem(sys.modules, 'rawpy', fake_rawpy)


def test_extract_rawpy_thumb_only_never_calls_postprocess_when_no_thumbnail(monkeypatch):
    postprocess_calls: list = []
    _install_fake_rawpy(monkeypatch, postprocess_calls, extract_thumb_raises=True)

    result = iu._extract_rawpy_thumb_only_from_path('/fake/path.cr3')

    assert result is None
    assert postprocess_calls == []


def test_extract_rawpy_preview_from_path_does_fall_back_to_postprocess(monkeypatch):
    # Contrast case, pinning the existing (intentional) behavior of the
    # *shrunk-preview* path this one must not share: when there's no embedded
    # thumbnail, _extract_rawpy_preview_from_path (used by the async
    # preview-generation worker, not the synchronous FR route) does fall
    # through to a real demosaic. If this assertion ever fails, the two code
    # paths have likely been merged and the FR route may have regained the
    # unbounded-postprocess() risk it was built to avoid.
    postprocess_calls: list = []
    _install_fake_rawpy(monkeypatch, postprocess_calls, extract_thumb_raises=True)

    iu._extract_rawpy_preview_from_path('/fake/path.cr3')

    assert postprocess_calls == [True]


def test_extract_raw_native_preview_bytes_uses_extension_specific_temp_file(monkeypatch, tmp_path):
    seen_paths: list = []

    def fake_extract_from_path(path):
        seen_paths.append(path)
        return b'jpeg-bytes'

    monkeypatch.setattr(iu, 'extract_raw_native_preview_from_path', fake_extract_from_path)

    result = iu.extract_raw_native_preview_bytes(b'not real raw bytes', 'IMG_1234.CR3')

    assert result == b'jpeg-bytes'
    assert len(seen_paths) == 1
    assert seen_paths[0].endswith('.cr3')


def test_extract_raw_native_preview_bytes_returns_none_on_failure(monkeypatch):
    def raising_extract(path):
        raise RuntimeError('boom')

    monkeypatch.setattr(iu, 'extract_raw_native_preview_from_path', raising_extract)

    assert iu.extract_raw_native_preview_bytes(b'bytes', 'IMG_1234.NEF') is None
