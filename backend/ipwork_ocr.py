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

import glob
import os
import queue
import threading
from typing import Dict, Optional


from image_utils import RAW_EXTENSIONS_CINEMA, RAW_EXTENSIONS_RAWPY, extract_raw_preview_bytes
from optional_deps import try_import
from ipwork_image import rgb_image

tesserocr = try_import('tesserocr')

MAX_OCR_TEXT_LENGTH = 2048

# tesserocr has no compiled-in default and falls back to a relative './' when
# neither an explicit path nor TESSDATA_PREFIX is set -- which silently fails
# PyTessBaseAPI() init in this container (apt's tesseract-ocr installs to
# /usr/share/tesseract-ocr/<major>/tessdata, not cwd), turning every single
# OCR call into the same 'no_data' result as a genuinely blank image. Resolved
# once at import time via glob (rather than hardcoding the major version)
# so an apt tesseract upgrade doesn't silently reintroduce this.
def _resolve_tessdata_path() -> Optional[str]:
    override = os.getenv('TESSDATA_PREFIX', '').strip()
    if override:
        return override
    for pattern in ('/usr/share/tesseract-ocr/*/tessdata', '/usr/share/tessdata'):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


_TESSDATA_PATH = _resolve_tessdata_path()
# Tesseract's own confidence scale is 0-100 (or -1 for non-text lines). This
# only drops individual low-confidence words -- not a recall-affecting PSM/OEM
# change like the ones already evaluated and rejected in
# docs/ipworker-architecture.md -- so it can only improve precision of what's
# already recognized, never find text PSM 3 would otherwise miss. Exposed as
# an env var (matching FACE_MIN_STORE_CONFIDENCE/YOLO_SCORE_THRESHOLD's
# pattern) since the right cutoff needs real-data validation this session
# doesn't have the corpus on hand to run.
OCR_MIN_WORD_CONFIDENCE = float(os.getenv('OCR_MIN_WORD_CONFIDENCE', '40'))

# Confirmed in production (2026-10-19): a single image hit the ipworker task
# watchdog's hard 120s ceiling on 4 independent, separately-dequeued attempts,
# always within ~10ms of exactly 120s -- libtesseract's Recognize() hung
# inside its C++ core with no cooperative-cancellation API, and since the
# watchdog's recovery is "kill the whole worker process, let the message
# redeliver," the same poison image re-hung every time and blocked the
# entire queue behind it indefinitely (2,368 backlogged messages observed).
# This bounds just the one call, well under that outer ceiling, so a hang
# fails this one photo's OCR step instead of the whole task/queue.
OCR_CALL_TIMEOUT_SECONDS = float(os.getenv('OCR_CALL_TIMEOUT_SECONDS', '30'))

_thread_local = threading.local()


def _get_api():
    api = getattr(_thread_local, 'api', None)
    if api is None:
        api = tesserocr.PyTessBaseAPI(path=_TESSDATA_PATH) if _TESSDATA_PATH else tesserocr.PyTessBaseAPI()
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
    # The preview-first runner retains the RAW filename while passing JPEG
    # bytes. Extracting a RAW preview again wastes subprocess I/O and can
    # select an embedded JPEG thumbnail instead of these actual pixels.
    if image_bytes.startswith(b'\xff\xd8'):
        return image_bytes
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return preview
    return image_bytes


def _recognize(image, outcome: 'queue.Queue') -> None:
    """Runs on its own daemon thread (see process_ocr) so a hang inside
    libtesseract's C++ core can be bounded. Puts exactly one
    (status, payload) pair on `outcome`; never raises out to the thread
    boundary since there's nothing on the other end to catch it once the
    caller has given up waiting."""
    try:
        api = _get_api()
        api.SetImage(image)
        if not api.Recognize():
            outcome.put(('error', RuntimeError('OCR recognition failed')))
            return
        iterator = api.GetIterator()
        if iterator is None:
            outcome.put(('error', RuntimeError('OCR result iterator unavailable')))
            return
        word_confidences = []
        level = tesserocr.RIL.WORD
        while True:
            # MapWordConfidences reads empty layout entries as words and
            # raises "No text returned" on valid text-free images.
            if not iterator.Empty(level):
                word_confidences.append((iterator.GetUTF8Text(level), iterator.Confidence(level)))
            if not iterator.Next(level):
                break
        outcome.put(('ok', word_confidences))
    except Exception as exc:
        outcome.put(('error', exc))


def process_ocr(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if tesserocr is None:
        return {'hasData': False, 'error': 'tesserocr_unavailable', 'errorCode': 'tesserocr_unavailable'}
    error_code = 'ocr_decode_failed'
    context = rgb_image(image_bytes, filename, _decodable_image_bytes)
    try:
        image = context.__enter__()
    except Exception as exc:
        return {'hasData': False, 'error': str(exc), 'errorCode': error_code}
    error_code = 'ocr_engine_failed'
    # Phone photos are commonly stored with an EXIF orientation tag rather
    # than physically rotated pixels (confirmed against real photos during
    # face-pipeline validation -- see ipwork_face.py); without correcting
    # for it, OCR would read sideways/upside-down text on any photo with a
    # non-default orientation. exif_transpose already applied inside
    # rgb_image()/_decode(), same as before this function was restructured.
    outcome: 'queue.Queue' = queue.Queue(maxsize=1)
    worker = threading.Thread(target=_recognize, args=(image, outcome), daemon=True)
    worker.start()
    try:
        status, payload = outcome.get(timeout=OCR_CALL_TIMEOUT_SECONDS)
    except queue.Empty:
        # Recognize() never returned within budget. tesserocr/libtesseract
        # has no cooperative-cancellation API, so `worker` is abandoned, not
        # joined or killed -- it leaks until the hang resolves or the
        # process exits. Deliberately skips context.__exit__() (which would
        # call image.close()): SetImage() may still hold a live reference
        # into that image from inside the stuck call, and closing it out
        # from under an abandoned thread risks a use-after-free crash in
        # native code instead of a clean, contained timeout. Leaking one
        # image's memory for a rare poison photo is the safer trade.
        return {'hasData': False, 'error': f'OCR recognition exceeded {OCR_CALL_TIMEOUT_SECONDS}s', 'errorCode': 'ocr_timeout'}
    try:
        context.__exit__(None, None, None)
    except Exception:
        pass
    if status == 'error':
        return {'hasData': False, 'error': str(payload), 'errorCode': error_code}
    word_confidences = payload
    # Word-level confidences (not full-page GetUTF8Text()) so low-confidence
    # words -- texture/grain tesseract hallucinated as "text", the dominant
    # false-positive mode documented in docs/ipworker-architecture.md's PSM
    # sweep -- can be dropped before they ever reach ocrText/search, without
    # touching recall for words it's actually confident about.
    words = [word for word, confidence in (word_confidences or []) if confidence >= OCR_MIN_WORD_CONFIDENCE and word.strip()]
    text = ' '.join(words).strip()
    if not text:
        return {'hasData': False}
    return {'hasData': True, 'text': text[:MAX_OCR_TEXT_LENGTH]}
