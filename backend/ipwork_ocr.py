"""ipworker's OCR step processor.

Mirrors the browser's tesseract.js OCR call (runBrowserOcr in
PhotoGallery.tsx) using tesserocr against the same tesseract engine,
running server-side. Requires the `tesseract-ocr` apt package (trained
data) and the `tesserocr` pip package -- both live only in the ipworker
image's requirements (requirements-ipworker.txt), so this module is
intentionally NOT imported by the plain backend/worker roles (see
app._register_ipwork_processors, which imports it lazily and only from
run_ipworker()).

Uses tesserocr's in-process PyTessBaseAPI instead of the pytesseract
subprocess wrapper this replaced (2026-08-28): pytesseract wrote each image
to a temp file and forked a fresh `tesseract` CLI process per call, which
reloads the trained data from disk on every single call. tesserocr links
directly against libtesseract, so one PyTessBaseAPI per worker thread loads
the trained data once and is reused for every subsequent photo processed on
that thread. PyTessBaseAPI is NOT thread-safe to share across threads --
with IPWORKER_CONCURRENCY=2 there are 2 worker threads calling
process_ocr() concurrently, so each gets its own instance via
threading.local() rather than a single module-level instance.

OMP_THREAD_LIMIT=1 (deploy/resources.bicep) still applies here even though
this is no longer a subprocess call: it's a container-level env var on the
ipworker process itself, and tesserocr's libtesseract runs in that same
process, so it reads the same os.environ OpenMP setting a spawned
subprocess would previously have inherited -- no code-level change needed
to keep that fix in effect.

Trade-off worth knowing: pytesseract's subprocess isolated a bad/corrupt
image to just that one subprocess. tesserocr runs libtesseract's C++ core
in-process, so a crash inside it (vs. a normal caught exception) would take
down the whole ipworker replica's process, not just this one OCR call.
"""
from __future__ import annotations

import io
import threading
from typing import Dict, Optional

from PIL import Image, ImageOps

try:
    import tesserocr
except Exception:  # pragma: no cover - only absent outside the ipworker image
    tesserocr = None

MAX_OCR_TEXT_LENGTH = 2048

_thread_local = threading.local()


def _get_api():
    api = getattr(_thread_local, 'api', None)
    if api is None:
        api = tesserocr.PyTessBaseAPI()
        _thread_local.api = api
    return api


def process_ocr(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if tesserocr is None:
        return {'hasData': False, 'error': 'tesserocr_unavailable'}
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            # Phone photos are commonly stored with an EXIF orientation tag
            # rather than physically rotated pixels (confirmed against real
            # photos during face-pipeline validation -- see ipwork_face.py);
            # without correcting for it, OCR would read sideways/upside-down
            # text on any photo with a non-default orientation.
            image = ImageOps.exif_transpose(image)
            api = _get_api()
            api.SetImage(image.convert('RGB'))
            text = api.GetUTF8Text()
    except Exception as exc:
        return {'hasData': False, 'error': str(exc)}
    text = (text or '').strip()
    if not text:
        return {'hasData': False}
    return {'hasData': True, 'text': text[:MAX_OCR_TEXT_LENGTH]}
