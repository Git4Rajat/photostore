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

from image_utils import RAW_EXTENSIONS_CINEMA, RAW_EXTENSIONS_RAWPY, extract_raw_preview_bytes
from optional_deps import try_import

tesserocr = try_import('tesserocr')

MAX_OCR_TEXT_LENGTH = 2048

_thread_local = threading.local()


def _get_api():
    api = getattr(_thread_local, 'api', None)
    if api is None:
        api = tesserocr.PyTessBaseAPI()
        _thread_local.api = api
    return api


def _decodable_image_bytes(image_bytes: bytes, filename: str) -> bytes:
    """See ipwork_face.py's copy of this helper: RAW formats (e.g. CR3) have no
    generic PIL codec, so Image.open() below raises 'cannot identify image
    file' on the raw bytes. Unlike ipwork_face.py/ipwork_vision.py, this module
    never got the same fallback -- 'preview' only lands in this same ipworker
    batch (and gets swapped into the shared image-bytes cache ahead of 'ocr')
    when it's still runnable; in 'both' mode the browser's client-processing
    request almost always finishes 'preview' synchronously first, so by the
    time the async ocr step runs it's excluded from that batch and this
    function would otherwise be handed the raw CR3 bytes directly. The
    resulting UnidentifiedImageError was being swallowed into the same
    terminal 'no_data' status as a genuine empty-OCR result (see
    storage_utils.py's ocr_result handling), permanently hiding real text."""
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return preview
    return image_bytes


def process_ocr(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if tesserocr is None:
        return {'hasData': False, 'error': 'tesserocr_unavailable'}
    try:
        with Image.open(io.BytesIO(_decodable_image_bytes(image_bytes, filename))) as image:
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
