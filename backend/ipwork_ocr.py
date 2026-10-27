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
directly against libtesseract, so one PyTessBaseAPI per resident OCR thread loads
the trained data once and is reused for every subsequent photo processed on
that thread. PyTessBaseAPI is NOT thread-safe to share across threads --
concurrent source workers borrow separate resident OCR workers, each owning an
instance via threading.local() rather than a shared module-level engine. Healthy
workers are reused; timed-out workers are retired and have bounded replacements.

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
import time
import processing_metrics as metrics
from typing import Dict, Optional


from image_utils import RAW_EXTENSIONS_CINEMA, RAW_EXTENSIONS_RAWPY, extract_raw_preview_bytes
from optional_deps import try_import
from ipwork_image import rgb_image, sharing_enabled

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
        started = time.monotonic()
        try:
            api = tesserocr.PyTessBaseAPI(path=_TESSDATA_PATH) if _TESSDATA_PATH else tesserocr.PyTessBaseAPI()
            _thread_local.api = api
        finally:
            metrics.add(ocr_engine_inits=1, ocr_engine_init_ms=(time.monotonic() - started) * 1000)
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


def _recognize(image):
    """Execute entirely on one reusable worker; API state never crosses threads."""
    api = None
    try:
        api = _get_api()
        api.SetImage(image)
        started = time.monotonic()
        try:
            recognized = api.Recognize()
        finally:
            metrics.add(ocr_recognition_ms=(time.monotonic() - started) * 1000)
        if not recognized:
            return 'error', RuntimeError('OCR recognition failed')
        iterator = api.GetIterator()
        if iterator is None:
            return 'error', RuntimeError('OCR result iterator unavailable')
        word_confidences = []
        level = tesserocr.RIL.WORD
        while True:
            # MapWordConfidences reads empty layout entries as words and
            # raises "No text returned" on valid text-free images.
            if not iterator.Empty(level):
                word_confidences.append((iterator.GetUTF8Text(level), iterator.Confidence(level)))
            if not iterator.Next(level):
                break
        return 'ok', word_confidences
    except Exception as exc:
        return 'error', exc
    finally:
        # Retain trained data, discard image/results and adaptation between
        # independent photos. Both operations stay on the engine's owner thread.
        if api is not None:
            clear = getattr(api, 'Clear', None)
            if callable(clear): clear()
            clear_adaptive = getattr(api, 'ClearAdaptiveClassifier', None)
            if callable(clear_adaptive): clear_adaptive()



class _OcrJob:
    def __init__(self, run, cleanup):
        self.run, self.cleanup = run, cleanup
        self.outcome = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.done = self.abandoned = False

    def execute(self):
        with self.lock:
            if self.abandoned:
                self.done = True
                self.cleanup()
                return
        try:
            result = self.run()
        except Exception as exc:
            result = 'error', exc
        with self.lock:
            self.done = True
            if self.abandoned:
                self.cleanup()
            else:
                self.outcome.put(result)

    def abandon(self):
        with self.lock:
            self.abandoned = True
            if self.done:
                self.cleanup()


class _OcrWorker:
    def __init__(self, pool):
        self.pool, self.retired = pool, False
        self.jobs = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, daemon=True, name='ocr-recognizer')

    def _run(self):
        try:
            while True:
                job = self.jobs.get()
                if job is None: break
                job.execute()
                del job  # Idle engines retain no image, trace context or request closure.
                with self.pool.lock:
                    if self.retired: break
        finally:
            api = getattr(_thread_local, 'api', None)
            try:
                if api is not None and callable(getattr(api, 'End', None)): api.End()
            except Exception:
                pass  # Engine is being discarded on its owning thread.
            finally:
                if api is not None: del _thread_local.api
                with self.pool.lock:
                    self.pool.workers.discard(self)


class _OcrPool:
    """Bounded resident engines plus spare slots for abandoned native calls.

    A timed-out worker is retired permanently. Its active native call retains
    its image until it returns, and is never given another photo. No native
    thread is killed, reused after timeout, or allowed unbounded replacements.
    """
    def __init__(self, limit):
        self.limit, self.lock = limit, threading.Lock()
        self.idle, self.workers = [], set()

    def acquire(self):
        with self.lock:
            if self.idle:
                metrics.add(ocr_worker_reuses=1)
                return self.idle.pop()
            if len(self.workers) >= self.limit:
                metrics.add(ocr_worker_capacity_errors=1)
                return None
            worker = _OcrWorker(self)
            self.workers.add(worker)
            try:
                worker.thread.start()
            except Exception:
                self.workers.remove(worker)
                raise
            metrics.add(ocr_worker_starts=1)
            return worker

    def release(self, worker):
        with self.lock:
            if not worker.retired: self.idle.append(worker)

    def retire(self, worker):
        with self.lock:
            if worker.retired: return
            worker.retired = True
            if worker in self.idle: self.idle.remove(worker)
            try: worker.jobs.put_nowait(None)
            except queue.Full: pass  # A job timed out before the native thread took it.

    def close(self):
        with self.lock: workers = list(self.workers)
        for worker in workers: self.retire(worker)
        for worker in workers: worker.thread.join(timeout=1)


_POOL = _OcrPool(max(1, int(os.getenv('IPWORKER_CONCURRENCY', '1'))) + 2)

def process_ocr(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if tesserocr is None:
        return {'hasData': False, 'error': 'tesserocr_unavailable', 'errorCode': 'tesserocr_unavailable'}
    error_code = 'ocr_decode_failed'
    context = rgb_image(image_bytes, filename, _decodable_image_bytes)
    try:
        image = context.__enter__()
        if sharing_enabled():
            # The source job can finish and close its shared pixels after an
            # OCR timeout. Give native recognition an independently owned copy.
            try:
                image = image.copy()
            finally:
                context.__exit__(None, None, None)
            cleanup = image.close
        else:
            cleanup = lambda: context.__exit__(None, None, None)
    except Exception as exc:
        return {'hasData': False, 'error': str(exc), 'errorCode': error_code}
    error_code = 'ocr_engine_failed'
    # Phone photos are commonly stored with an EXIF orientation tag rather
    # than physically rotated pixels (confirmed against real photos during
    # face-pipeline validation -- see ipwork_face.py); without correcting
    # for it, OCR would read sideways/upside-down text on any photo with a
    # non-default orientation. exif_transpose already applied inside
    # rgb_image()/_decode(), same as before this function was restructured.
    def close_image():
        try: cleanup()
        except Exception: pass
    try:
        worker = _POOL.acquire()
    except Exception as exc:
        close_image()
        return {'hasData': False, 'error': str(exc), 'errorCode': error_code}
    if worker is None:
        close_image()
        return {'hasData': False, 'error': 'OCR worker capacity exhausted', 'errorCode': error_code}
    job = _OcrJob(metrics.inherit(lambda: _recognize(image), purpose_name='other'), close_image)
    worker.jobs.put_nowait(job)
    try:
        status, payload = job.outcome.get(timeout=OCR_CALL_TIMEOUT_SECONDS)
    except queue.Empty:
        metrics.add(ocr_worker_timeouts=1)
        job.abandon()
        _POOL.retire(worker)
        return {'hasData': False, 'error': f'OCR recognition exceeded {OCR_CALL_TIMEOUT_SECONDS}s', 'errorCode': 'ocr_timeout'}
    close_image()
    if status == 'error':
        _POOL.retire(worker)
        return {'hasData': False, 'error': str(payload), 'errorCode': error_code}
    _POOL.release(worker)
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
