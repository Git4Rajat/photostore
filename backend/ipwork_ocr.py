"""ipworker's OCR step processor.

Mirrors the browser's tesseract.js OCR call (runBrowserOcr in
PhotoGallery.tsx) using pytesseract against the same tesseract engine,
running server-side. Requires the `tesseract-ocr` apt package and the
`pytesseract` pip package -- both live only in the ipworker image's
requirements (requirements-ipworker.txt), so this module is intentionally
NOT imported by the plain backend/worker roles (see
app._register_ipwork_processors, which imports it lazily and only from
run_ipworker()).
"""
from __future__ import annotations

import io
from typing import Dict, Optional

from PIL import Image, ImageOps

try:
    import pytesseract
except Exception:  # pragma: no cover - only absent outside the ipworker image
    pytesseract = None

MAX_OCR_TEXT_LENGTH = 2048


def process_ocr(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if pytesseract is None:
        return {'hasData': False, 'error': 'pytesseract_unavailable'}
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            # Phone photos are commonly stored with an EXIF orientation tag
            # rather than physically rotated pixels (confirmed against real
            # photos during face-pipeline validation -- see ipwork_face.py);
            # without correcting for it, OCR would read sideways/upside-down
            # text on any photo with a non-default orientation.
            image = ImageOps.exif_transpose(image)
            text = pytesseract.image_to_string(image.convert('RGB'))
    except Exception as exc:
        return {'hasData': False, 'error': str(exc)}
    text = (text or '').strip()
    if not text:
        return {'hasData': False}
    return {'hasData': True, 'text': text[:MAX_OCR_TEXT_LENGTH]}
