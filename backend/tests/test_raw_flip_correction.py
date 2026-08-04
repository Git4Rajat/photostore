"""Regression test for the RAW-preview orientation fix.

Embedded preview/thumbnail JPEGs pulled out of a RAW container (CR3 in
particular) carry no reliable orientation tag of their own -- verified
against real CR3 files, where the largest embedded preview is always
EXIF Orientation=1 regardless of the shot's actual rotation. image_utils
corrects for this using LibRaw's own `raw.sizes.flip` value instead of
trusting the embedded JPEG's tag. The flip->rotation mapping itself was
verified against real, unambiguous photos (people, buildings, horizons)
rather than assumed from the EXIF-orientation-tag analogy, since that
analogy turned out to invert the mapping.
"""
from __future__ import annotations

import io

from PIL import Image

import image_utils as iu


def _marker_jpeg(width: int, height: int) -> bytes:
    """A width x height image with a distinct red marker in the top-left
    corner, so a transpose's direction can be read back off the result."""
    image = Image.new('RGB', (width, height), (0, 0, 0))
    marker_size = max(1, min(width, height) // 4)
    for x in range(marker_size):
        for y in range(marker_size):
            image.putpixel((x, y), (255, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format='JPEG', quality=95)
    return buf.getvalue()


def _corner_with_marker(jpeg_bytes: bytes) -> str:
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        w, h = image.size
        m = max(1, min(w, h) // 8)
        corners = {
            'top-left': image.getpixel((m // 2, m // 2)),
            'top-right': image.getpixel((w - 1 - m // 2, m // 2)),
            'bottom-left': image.getpixel((m // 2, h - 1 - m // 2)),
            'bottom-right': image.getpixel((w - 1 - m // 2, h - 1 - m // 2)),
        }
        reddest = max(corners, key=lambda k: corners[k][0] - corners[k][1] - corners[k][2])
        return reddest


def test_apply_raw_flip_noop_for_flip_zero():
    original = _marker_jpeg(80, 40)
    assert iu._apply_raw_flip(original, 0) == original


def test_apply_raw_flip_noop_for_unknown_flip_value():
    original = _marker_jpeg(80, 40)
    assert iu._apply_raw_flip(original, 99) == original


def test_apply_raw_flip_none_bytes():
    assert iu._apply_raw_flip(None, 6) is None
    assert iu._apply_raw_flip(b'', 5) == b''


def test_apply_raw_flip_180_moves_marker_to_opposite_corner():
    source = _marker_jpeg(80, 40)
    assert _corner_with_marker(source) == 'top-left'
    rotated = iu._apply_raw_flip(source, 3)
    assert _corner_with_marker(rotated) == 'bottom-right'


def test_apply_raw_flip_5_and_6_are_opposite_quarter_turns():
    # Verified empirically against real CR3 photos (people/buildings/horizons,
    # not wildlife -- a bird hanging from a branch is a legitimate pose and
    # made an earlier manual check misleading): flip=5 and flip=6 must land
    # the marker on opposite corners, since they're opposite-direction
    # quarter turns of the same sensor-orientation source.
    source = _marker_jpeg(80, 40)
    rotated_5 = iu._apply_raw_flip(source, 5)
    rotated_6 = iu._apply_raw_flip(source, 6)
    with Image.open(io.BytesIO(rotated_5)) as image:
        assert image.size == (40, 80)
    with Image.open(io.BytesIO(rotated_6)) as image:
        assert image.size == (40, 80)
    assert _corner_with_marker(rotated_5) != _corner_with_marker(rotated_6)
    corner_5 = _corner_with_marker(rotated_5)
    corner_6 = _corner_with_marker(rotated_6)
    assert {corner_5, corner_6} == {'top-right', 'bottom-left'}


def test_raw_container_flip_from_path_returns_zero_when_unreadable(tmp_path):
    bogus = tmp_path / 'not_a_raw_file.cr3'
    bogus.write_bytes(b'not actually a raw file')
    assert iu._raw_container_flip_from_path(str(bogus)) == 0


def test_raw_container_flip_from_bytes_returns_zero_when_unreadable():
    assert iu._raw_container_flip_from_bytes(b'not actually a raw file') == 0
