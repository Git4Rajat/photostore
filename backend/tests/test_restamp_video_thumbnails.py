import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import restamp_video_thumbnails as restamp


def test_thumbnail_size_follows_the_upload_rule():
    assert restamp.thumbnail_size(1920, 1080) == (120, 68)
    assert restamp.thumbnail_size(1080, 1920) == (68, 120)
    assert restamp.thumbnail_size(60, 40) == (60, 40)   # never upscaled


def test_play_icon_is_drawn_in_the_centre_only():
    frame = Image.new('RGB', (1920, 1080), (200, 30, 30))
    stamped = restamp.stamp_play_icon(frame)
    assert stamped.size == (120, 68)
    assert stamped.getpixel((2, 2)) == (200, 30, 30)            # corners (where the old pill sat) untouched
    assert stamped.getpixel((4, 64)) == (200, 30, 30)
    assert min(stamped.getpixel((61, 34))) > 220                # triangle is white near the centre
    ring = stamped.getpixel((60 - 10, 34))                      # disc darkens the frame around the triangle
    assert ring[0] < 200


def test_encoded_thumbnail_is_a_bounded_jpeg():
    data = restamp.encode_thumbnail(Image.new('RGB', (4000, 3000), (10, 120, 200)))
    image = Image.open(io.BytesIO(data))
    assert image.format == 'JPEG' and max(image.size) == restamp.THUMBNAIL_EDGE
    assert len(data) < 512_000
