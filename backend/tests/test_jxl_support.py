"""Regression coverage for JPEG XL (.jxl) upload support.

Previously .jxl was missing from ALLOWED_EXTENSIONS, so
_validate_media_filename('IMG_7841.JXL') returned None and every JXL upload
was rejected with a generic 'Invalid filename' 400 before any real
filename-safety check ran. pillow-jxl-plugin now registers a decoder with
Pillow (see image_utils.py's import block), so JXL flows through the same
Image.open()-based paths as webp/png/etc.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

import image_utils


def _jxl_bytes(size=(64, 48), color=(10, 120, 200)) -> bytes:
    image = Image.new('RGB', size, color=color)
    buffer = io.BytesIO()
    image.save(buffer, format='JXL')
    return buffer.getvalue()


def test_jxl_extension_allowed():
    assert image_utils.allowed_file('IMG_7841.JXL') is True
    assert image_utils.allowed_file('IMG_7841.jxl') is True


def test_jxl_requires_backend_preview():
    # Browsers can't render JXL via <img src>, so like HEIC/RAW it must be
    # converted server-side before the frontend can display it.
    assert 'jxl' in image_utils.BROWSER_UNVIEWABLE_EXTENSIONS


def test_jxl_verify_image_accepts_real_file():
    assert image_utils.verify_image(_jxl_bytes(), 'IMG_7841.JXL') is None


def test_jxl_verify_image_rejects_garbage():
    err = image_utils.verify_image(b'not a real jxl file', 'IMG_7841.JXL')
    assert err is not None


def test_jxl_thumbnail_generation():
    thumb_bytes = image_utils.create_thumbnail_data(_jxl_bytes())
    with Image.open(io.BytesIO(thumb_bytes)) as thumb:
        assert thumb.format == 'JPEG'
        assert max(thumb.size) <= max(image_utils.THUMBNAIL_SIZE)


def test_jxl_convert_to_jpeg_preview():
    converted = image_utils.convert_image_to_jpeg(_jxl_bytes(size=(800, 600)), 'IMG_7841.JXL')
    with Image.open(io.BytesIO(converted)) as preview:
        assert preview.format == 'JPEG'
        assert preview.size == (800, 600)
