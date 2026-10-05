"""Coverage for image_utils.crop_face_thumbnail -- the pixel math shared
between routes/people.py's on-demand face_crop() route and
storage_utils._warm_face_crops_for_photo's background pre-generation, so both
produce byte-identical crops for the same face."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from image_utils import crop_face_thumbnail


def _make_jpeg(width: int, height: int, color=(200, 100, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buf, format='JPEG')
    return buf.getvalue()


def test_crop_face_thumbnail_returns_valid_jpeg_within_bbox():
    source = _make_jpeg(400, 400)
    bbox = {'left': 100, 'top': 100, 'width': 80, 'height': 80}

    result = crop_face_thumbnail(source, 'a.jpg', bbox, img_w=400, img_h=400, rotation=0)

    assert result is not None
    with Image.open(io.BytesIO(result)) as img:
        assert img.format == 'JPEG'
        # Padded crop (35%) of an 80x80 box, capped at 512x512.
        assert img.width <= 512 and img.height <= 512
        assert img.width > 80 and img.height > 80


def test_crop_face_thumbnail_scales_bbox_when_source_resolution_differs():
    # bbox coordinates are in the ORIGINAL detection resolution (img_w/img_h);
    # the actual source bytes here are half that resolution, exercising the
    # sx/sy scale factors.
    source = _make_jpeg(200, 200)
    bbox = {'left': 100, 'top': 100, 'width': 80, 'height': 80}

    result = crop_face_thumbnail(source, 'a.jpg', bbox, img_w=400, img_h=400, rotation=0)

    assert result is not None


def test_crop_face_thumbnail_none_on_invalid_bbox():
    source = _make_jpeg(200, 200)
    assert crop_face_thumbnail(source, 'a.jpg', {'left': 0, 'top': 0, 'width': 0, 'height': 0}, 200, 200) is None


def test_crop_face_thumbnail_none_on_zero_image_dimensions():
    source = _make_jpeg(200, 200)
    bbox = {'left': 10, 'top': 10, 'width': 50, 'height': 50}
    assert crop_face_thumbnail(source, 'a.jpg', bbox, img_w=0, img_h=0) is None


def test_crop_face_thumbnail_none_on_undecodable_bytes():
    assert crop_face_thumbnail(b'not-an-image', 'a.jpg', {'left': 0, 'top': 0, 'width': 10, 'height': 10}, 100, 100) is None


def test_crop_face_thumbnail_applies_manual_rotation():
    source = _make_jpeg(400, 200)
    bbox = {'left': 50, 'top': 50, 'width': 60, 'height': 60}

    upright = crop_face_thumbnail(source, 'a.jpg', bbox, img_w=400, img_h=200, rotation=0)
    rotated = crop_face_thumbnail(source, 'a.jpg', bbox, img_w=400, img_h=200, rotation=90)

    assert upright is not None and rotated is not None
