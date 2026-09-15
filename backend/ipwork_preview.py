"""ipworker's preview step processor.

Generates the shrunk (~2048px / ~1MB) JPEG preview shown by default in the
lightbox for every photo, not just RAW/HEIC/JXL. Reuses convert_image_to_jpeg
(image_utils.py) -- the same encoder the RAW/HEIC-only preview route has used
all along, just applied universally now instead of gated by
_filename_requires_backend_preview.

Deliberately meant to run first when requested: _run_ipwork_steps swaps its
output bytes in for the cached original once this step succeeds, so every
later step in the same call (thumbnail/face/ocr/ai_vision) decodes a ~2048px
JPEG instead of the full original -- see _run_ipwork_steps in app.py. Mirrors
ipwork_thumbnail.py's shape/signature.
"""
from __future__ import annotations

import base64
from typing import Dict, Optional

from image_utils import convert_image_to_jpeg, is_video_file


def _looks_like_jpeg_bytes(data: bytes) -> bool:
    return bool(data) and data.startswith(b'\xff\xd8')


def process_preview(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    # Videos already get a poster-frame thumbnail via the video-specific path
    # (create_video_thumbnail_data); a resized-JPEG "preview" doesn't apply.
    if is_video_file(filename):
        return {'hasData': False}
    try:
        preview_bytes = convert_image_to_jpeg(image_bytes, filename)
    except Exception as exc:
        return {'hasData': False, 'error': str(exc)}
    if not preview_bytes or not _looks_like_jpeg_bytes(preview_bytes):
        return {'hasData': False}
    return {
        'hasData': True,
        'contentType': 'image/jpeg',
        'data': base64.b64encode(preview_bytes).decode('ascii'),
    }
