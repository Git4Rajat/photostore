"""Unit tests for ipwork_thumbnail.py -- ipworker's server-side thumbnail step,
added so 'backend' mode reprocessing/backfill doesn't need a live browser tab
just to regenerate thumbnails (the one step that used to be a permanent
browser-only exception; see IPWORK_STEPS's comment in app.py). Uses the same
image_utils helpers as storage_utils's existing reactive server-side
thumbnail fallback, so no ipworker-only deps are needed to test this module.
"""
from __future__ import annotations

import base64
import io

from PIL import Image

import ipwork_thumbnail


def _jpeg_bytes(size=(640, 480), color=(200, 50, 50)) -> bytes:
    image = Image.new('RGB', size, color=color)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG')
    return buffer.getvalue()


def test_process_thumbnail_returns_small_jpeg_for_ordinary_photo():
    result = ipwork_thumbnail.process_thumbnail('lib-A', 'photo.jpg', _jpeg_bytes())

    assert result['hasData'] is True
    assert result['contentType'] == 'image/jpeg'
    thumbnail_bytes = base64.b64decode(result['data'])
    assert thumbnail_bytes.startswith(b'\xff\xd8')
    with Image.open(io.BytesIO(thumbnail_bytes)) as thumb:
        assert thumb.width <= 120
        assert thumb.height <= 120


def test_process_thumbnail_falls_back_to_placeholder_for_garbage_bytes():
    # create_thumbnail_data (image_utils.py) never gives up for an "ordinary"
    # extension -- undecodable bytes still produce create_placeholder_thumbnail(),
    # matching storage_utils's existing reactive server-side fallback behavior.
    # A genuine "nothing to show" result only happens for RAW/HEIF/video paths
    # that can't even extract a preview frame (see extract_raw_preview_bytes /
    # create_video_thumbnail_data returning None).
    result = ipwork_thumbnail.process_thumbnail('lib-A', 'photo.jpg', b'not an image')
    assert result['hasData'] is True
    thumbnail_bytes = base64.b64decode(result['data'])
    assert thumbnail_bytes.startswith(b'\xff\xd8')


def test_process_thumbnail_never_raises_on_broken_input():
    result = ipwork_thumbnail.process_thumbnail('lib-A', 'photo.jpg', b'')
    assert isinstance(result, dict)
    assert 'hasData' in result


def test_process_thumbnail_routes_video_extension_through_video_path(monkeypatch):
    # create_video_thumbnail_data is mocked out, so this doesn't touch ffmpeg --
    # it only confirms is_video_file() routing sends .mp4 through the video
    # path rather than create_thumbnail_data (which would try to open it as a
    # still image and, per the fallback test above, "succeed" with a
    # placeholder instead of correctly reporting no data).
    calls = []
    monkeypatch.setattr(ipwork_thumbnail, 'create_video_thumbnail_data', lambda data, filename: calls.append((data, filename)) or None)

    result = ipwork_thumbnail.process_thumbnail('lib-A', 'clip.mp4', b'not-a-real-video')

    assert len(calls) == 1
    assert calls[0][1] == 'clip.mp4'
    assert result == {'hasData': False}
