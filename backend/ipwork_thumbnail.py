"""ipworker's thumbnail step processor.

Mirrors the browser's client-side canvas-resize thumbnail (createBrowserThumbnail/
createVideoBrowserThumbnail in PhotoGallery.tsx) using the exact same
image_utils helpers the backend's own reactive server-side thumbnail fallback
already relies on (storage_utils._create_server_thumbnail_for_upload) --
duplicated here rather than imported so this module stays a self-contained
leaf like the other ipwork_*.py processors (no storage_utils/_CTX coupling).
Needs only what backend/requirements.txt already provides (Pillow, rawpy,
ffmpeg on PATH), so unlike ipwork_face/ipwork_vision/ipwork_ocr this one has
no ipworker-only pip dependency -- it's just never wired into the plain
backend/worker roles because thumbnails there are still expected to arrive
from the browser (or the existing reactive fallback) in 'browser' mode.
"""
from __future__ import annotations

import base64
from typing import Dict, Optional

from image_utils import (
    RAW_EXTENSIONS_CINEMA,
    RAW_EXTENSIONS_RAWPY,
    convert_image_to_jpeg,
    create_thumbnail_data,
    create_video_thumbnail_data,
    extract_raw_preview_bytes,
    is_video_file,
)

HEIF_EXTENSIONS = {'heic', 'heif'}


def _filename_extension(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''


def _looks_like_jpeg_bytes(data: bytes) -> bool:
    return bool(data) and data.startswith(b'\xff\xd8')


def _create_thumbnail_bytes(image_bytes: bytes, filename: str) -> Optional[bytes]:
    if is_video_file(filename):
        return create_video_thumbnail_data(image_bytes, filename)
    ext = _filename_extension(filename)
    if ext in HEIF_EXTENSIONS:
        preview_bytes = convert_image_to_jpeg(image_bytes, filename)
        if not _looks_like_jpeg_bytes(preview_bytes):
            return None
        return create_thumbnail_data(preview_bytes)
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview_bytes = extract_raw_preview_bytes(image_bytes, filename)
        if not preview_bytes or not _looks_like_jpeg_bytes(preview_bytes):
            return None
        return create_thumbnail_data(preview_bytes)
    return create_thumbnail_data(image_bytes)


def process_thumbnail(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    try:
        thumbnail_bytes = _create_thumbnail_bytes(image_bytes, filename)
    except Exception as exc:
        return {'hasData': False, 'error': str(exc)}
    if not thumbnail_bytes:
        return {'hasData': False}
    return {
        'hasData': True,
        'contentType': 'image/jpeg',
        'data': base64.b64encode(thumbnail_bytes).decode('ascii'),
    }
