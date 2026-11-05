import io
import os
import re
import threading
import time
import processing_metrics as metrics
import unicodedata
from typing import Dict, List, Optional

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

try:
    import torch
    import open_clip
    from PIL import Image, ImageOps
except Exception:
    torch = None
    open_clip = None

if torch is not None:
    # Confirmed live 2026-08-27: torch.get_num_threads() is unaffected by
    # OMP_THREAD_LIMIT (the env var already set for ipworker's tesseract
    # fix) -- that only bounds OpenMP-based libraries in a way this build's
    # intra-op thread pool doesn't observably respect. At
    # IPWORKER_CONCURRENCY=2, two worker threads each running CLIP inference
    # with an unconstrained thread pool is the same CPU-oversubscription bug
    # already fixed once for tesseract; set it explicitly here instead of
    # relying on an env var with unverified effect on this library. See
    # docs/ipworker-architecture.md for the writeup.
    torch.set_num_threads(1)

_MODEL = None
_TOKENIZER = None
_PREPROCESS = None
_MODEL_NAME = ''
_MODEL_PRETRAINED = ''

# PyTorch's plain nn.Module.forward() is not documented safe for concurrent
# calls from multiple threads on one shared module instance (unlike
# onnxruntime's InferenceSession.Run(), which is explicitly documented
# reentrant) -- serialize just the forward() calls below so concurrent
# ipworker threads can't corrupt each other's inference.
_MODEL_LOCK = threading.Lock()

# Opt-in: run the per-image CLIP image/text encode through a one-thread FP32
# ONNX session instead of PyTorch. Same FP32 weights, same preprocessing, same
# normalization and shared lock -- only the forward() kernel changes. A local
# matched benchmark measured ~7% less wall AND less CPU per encode at identical
# outputs (cosine > 0.999999); see docs/benchmarks/vision-next-gains-independent
# -20261029.md. Fixed batch size 1 on purpose: a dynamic batch axis regressed
# the single-image case (same effect seen for AdaFace), so only the single-item
# hot paths route here; the rarely-used batch/vocab encoders stay on PyTorch.
# Default off: enabling it is the matched two-vCPU validation the benchmark
# flagged as the required next step, and any export/load failure falls back to
# PyTorch so the flag can never break encoding.
_ONNX_IMAGE = None
_ONNX_TEXT = None
_ONNX_LOADED: Optional[bool] = None
_ONNX_LOCK = threading.Lock()


def _clip_onnx_enabled() -> bool:
    return os.getenv('IPWORK_CLIP_ONNX', '').strip().lower() in ('1', 'true', 'yes')


def _load_onnx_towers() -> bool:
    """Export the image/text towers to fixed-batch-1 FP32 ONNX once per process
    and build one-thread sessions. Returns False (and leaves encoding on
    PyTorch) when disabled or if export/load fails for any reason."""
    global _ONNX_IMAGE, _ONNX_TEXT, _ONNX_LOADED
    if not _clip_onnx_enabled():
        return False
    if _ONNX_LOADED is not None:
        return _ONNX_LOADED
    with _ONNX_LOCK:
        if _ONNX_LOADED is not None:
            return _ONNX_LOADED
        try:
            import tempfile
            import onnxruntime as ort
            if not _load_model():
                raise RuntimeError('CLIP model unavailable for ONNX export')

            class _Tower(torch.nn.Module):
                def __init__(self, model, kind):
                    super().__init__()
                    self.model, self.kind = model, kind

                def forward(self, value):
                    # Raw (un-normalized) features: the callers normalize, so
                    # this stays numerically identical to the PyTorch path.
                    return (self.model.encode_image(value) if self.kind == 'image'
                            else self.model.encode_text(value))

            directory = tempfile.mkdtemp(prefix='photostore-clip-onnx-')
            example = {'image': _PREPROCESS(Image.new('RGB', (224, 224))).unsqueeze(0).to(_device()),
                       'text': _TOKENIZER(['a']).to(_device())}
            # The legacy exporter needs the decomposed attention path rather than
            # the CPU fast-path native-attention operator; live inference keeps
            # its normal fast path.
            fastpath = torch.backends.mha.get_fastpath_enabled()
            torch.backends.mha.set_fastpath_enabled(False)
            try:
                with torch.no_grad():
                    for kind in ('image', 'text'):
                        torch.onnx.export(_Tower(_MODEL, kind).eval(), (example[kind],),
                            os.path.join(directory, kind + '.onnx'), input_names=['input'],
                            output_names=['features'], opset_version=17, dynamo=False)
            finally:
                torch.backends.mha.set_fastpath_enabled(fastpath)
            options = ort.SessionOptions()
            options.intra_op_num_threads, options.inter_op_num_threads = 1, 1
            _ONNX_IMAGE = ort.InferenceSession(os.path.join(directory, 'image.onnx'),
                sess_options=options, providers=['CPUExecutionProvider'])
            _ONNX_TEXT = ort.InferenceSession(os.path.join(directory, 'text.onnx'),
                sess_options=options, providers=['CPUExecutionProvider'])
            _ONNX_LOADED = True
        except Exception:
            import logging
            logging.getLogger(__name__).warning('ONNX CLIP towers unavailable; using PyTorch', exc_info=True)
            _ONNX_IMAGE = _ONNX_TEXT = None
            _ONNX_LOADED = False
        return _ONNX_LOADED


def _image_tower_features(pixels):
    """encode_image for a single prepared [1,3,H,W] tensor, via ONNX when
    enabled and loaded, else PyTorch. Returns raw (un-normalized) features."""
    if _load_onnx_towers():
        output = _ONNX_IMAGE.run(['features'], {'input': pixels.detach().cpu().numpy()})[0]
        return torch.from_numpy(output)
    return _MODEL.encode_image(pixels)


def _text_tower_features(tokens):
    """encode_text for a single prepared [1,T] token tensor, via ONNX when
    enabled and loaded, else PyTorch. Returns raw (un-normalized) features."""
    if _load_onnx_towers():
        output = _ONNX_TEXT.run(['features'], {'input': tokens.detach().cpu().numpy()})[0]
        return torch.from_numpy(output)
    return _MODEL.encode_text(tokens)

_FALLBACK_EMBEDDING_DIMS = max(256, int(os.getenv('TEXT_EMBEDDING_FALLBACK_DIMS', '1024')))
if _FALLBACK_EMBEDDING_DIMS % 2:
    _FALLBACK_EMBEDDING_DIMS += 1
_WORD_HASHER = HashingVectorizer(
    n_features=_FALLBACK_EMBEDDING_DIMS // 2,
    alternate_sign=False,
    norm='l2',
    analyzer='word',
    ngram_range=(1, 2),
    lowercase=True,
    token_pattern=r'(?u)\b\w+\b',
)
_CHAR_HASHER = HashingVectorizer(
    n_features=_FALLBACK_EMBEDDING_DIMS // 2,
    alternate_sign=False,
    norm='l2',
    analyzer='char_wb',
    ngram_range=(3, 5),
    lowercase=True,
)


def _normalize_embedding_text(text: str) -> str:
    folded = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii')
    cleaned = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', folded.lower())).strip()
    return cleaned


def _device():
    if torch is not None and torch.cuda.is_available():
        return 'cuda'
    return 'cpu'


def _load_model() -> bool:
    global _MODEL, _TOKENIZER, _PREPROCESS, _MODEL_NAME, _MODEL_PRETRAINED
    if torch is None or open_clip is None:
        return False
    if _MODEL is None or _TOKENIZER is None:
        try:
            # Must match the CLIP checkpoint used for browser-side image embeddings
            # (Xenova/clip-vit-base-patch32, exported from openai/clip-vit-base-patch32)
            # so query text embeddings and photo image embeddings share one vector space.
            # open_clip's plain "ViT-B-32" config defaults to standard GELU; the
            # original openai checkpoint used QuickGELU, so it must be loaded via the
            # "-quickgelu" variant or the loaded weights don't match the architecture
            # they were trained with (open_clip warns about exactly this mismatch).
            model_name = os.getenv('OPENCLIP_MODEL', 'ViT-B-32-quickgelu')
            pretrained = os.getenv('OPENCLIP_PRETRAINED', 'openai')
            model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
            model = model.to(_device())
            model.eval()
            _MODEL = model
            _TOKENIZER = open_clip.get_tokenizer(model_name)
            _PREPROCESS = preprocess
            _MODEL_NAME = model_name
            _MODEL_PRETRAINED = pretrained
        except Exception:
            return False
    return True


def _hash_text_embedding(text: str) -> List[float]:
    clean = _normalize_embedding_text(text)
    if not clean:
        return []
    try:
        word = _WORD_HASHER.transform([clean]).toarray()[0]
        char = _CHAR_HASHER.transform([clean]).toarray()[0]
        embedding = np.concatenate([word, char]).astype(np.float32, copy=False)
        norm = float(np.linalg.norm(embedding))
        if norm <= 0:
            return []
        embedding /= norm
        return embedding.tolist()
    except Exception:
        return []


def get_text_embedding_version() -> str:
    if _load_model():
        return f'openclip:{_MODEL_NAME}:{_MODEL_PRETRAINED}'
    return f'hashing-v1:{_FALLBACK_EMBEDDING_DIMS}'


def get_text_embedding_dimension() -> int:
    if _load_model():
        return 512
    return _FALLBACK_EMBEDDING_DIMS


# Server-side embeddings are limited to text queries for search.
def encode_text_embedding(text: str) -> List[float]:
    from semantic_batching import current_batcher
    batcher = current_batcher()
    if text and batcher is not None:
        return batcher.submit(text)
    started = time.monotonic()
    try:
        return _encode_text_embedding_single(text)
    finally:
        metrics.add(semantic_text_requests=1, semantic_text_request_ms=(time.monotonic()-started)*1000)


def _encode_text_embedding_single(text: str) -> List[float]:
    if not text:
        return []
    if not _load_model():
        return _hash_text_embedding(text)
    try:
        started = time.monotonic()
        tokens = _TOKENIZER([text]).to(_device())
        metrics.add(text_tokenize_ms=(time.monotonic() - started) * 1000)
        with torch.no_grad():
            waiting = time.monotonic()
            with _MODEL_LOCK:
                metrics.add(text_lock_wait_ms=(time.monotonic() - waiting) * 1000)
                started = time.monotonic()
                try:
                    text_features = _text_tower_features(tokens)
                finally:
                    metrics.add(text_inference_ms=(time.monotonic() - started) * 1000)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        return text_features.squeeze(0).cpu().tolist()
    except Exception:
        return _hash_text_embedding(text)


def image_encoder_available() -> bool:
    """True if the real CLIP image tower loaded (vs. the hashing fallback,
    which is text-only -- there is no image equivalent of it)."""
    return _load_model()


# ipworker's vision step (see ipwork_vision.py): the browser writes
# `photoEmbedding` from the same CLIP checkpoint (Xenova/clip-vit-base-patch32),
# so this must stay in the exact same vector space as encode_text_embedding
# above and PHOTO_EMBEDDING_MODEL_VERSION (app.py) for semantic search to work
# across browser- and server-computed embeddings alike.
def encode_image_embedding(image_bytes: bytes) -> List[float]:
    if not _load_model():
        return []
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            # See ipwork_face.py's process_face comment: phone photos commonly
            # carry an EXIF orientation tag rather than physically rotated
            # pixels, and CLIP's tagging/embedding is not orientation-invariant
            # (a sideways portrait can genuinely confuse "portrait"/person tags).
            with ImageOps.exif_transpose(image) as oriented:
                with oriented.convert('RGB') as rgb:
                    return encode_rgb_image_embedding(rgb)
    except Exception:
        return []


def encode_rgb_image_embedding(image) -> List[float]:
    """Encode already-oriented RGB pixels without decoding JPEG again."""
    if not _load_model():
        return []
    try:
        started = time.monotonic()
        pixel_values = _PREPROCESS(image).unsqueeze(0).to(_device())
        metrics.add(clip_preprocess_ms=(time.monotonic() - started) * 1000)
        from image_batching import current_batcher
        batcher = current_batcher()
        return (batcher.submit(pixel_values) if batcher is not None
                else _encode_image_tensor_single(pixel_values))
    except Exception:
        return []


def _encode_image_tensors_batch(tensors):
    """Prepared tensors only; no source context, image decoding or persistence."""
    started = time.monotonic()
    pixels = torch.cat(tensors, dim=0)
    metrics.add(clip_image_stack_ms=(time.monotonic() - started) * 1000)
    with torch.no_grad():
        waiting = time.monotonic()
        with _MODEL_LOCK:
            metrics.add(clip_lock_wait_ms=(time.monotonic() - waiting) * 1000)
            started = time.monotonic()
            try:
                features = _MODEL.encode_image(pixels)
            finally:
                metrics.add(clip_inference_ms=(time.monotonic() - started) * 1000)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.cpu().tolist()


def _encode_image_tensor_single(tensor):
    # Same numerical path as the existing singleton encoder, without a copy.
    with torch.no_grad():
        waiting = time.monotonic()
        with _MODEL_LOCK:
            metrics.add(clip_lock_wait_ms=(time.monotonic() - waiting) * 1000)
            started = time.monotonic()
            try:
                features = _image_tower_features(tensor)
            finally:
                metrics.add(clip_inference_ms=(time.monotonic() - started) * 1000)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).cpu().tolist()


def image_embedding_batches(*, source_workers=2, batch_size=None, window=None):
    """Opt-in per-attempt scope; a single source never waits to fill a batch."""
    from contextlib import nullcontext
    from image_batching import image_batches
    size = int(os.getenv('IPWORK_CLIP_IMAGE_BATCH_SIZE', '1')) if batch_size is None else batch_size
    wait = float(os.getenv('IPWORK_CLIP_IMAGE_BATCH_WINDOW_MS', '2')) / 1000 if window is None else window
    if type(size) is not int or not 1 <= size <= 8 or not 0 <= wait <= .05:
        raise ValueError('Invalid CLIP image batching configuration')
    size = min(size, source_workers)
    return (nullcontext() if size <= 1 else image_batches(
        _encode_image_tensors_batch, _encode_image_tensor_single, batch_size=size, window=wait))


# Precomputed once per process and cached by ipwork_vision.py -- encoding the
# vocabulary's ~500 labels through the text tower on every single photo would
# be far more wasteful than once at process startup, since the vocabulary
# itself never changes at runtime.
def encode_text_embeddings_batch(texts: List[str]) -> List[List[float]]:
    if not texts or not _load_model():
        return []
    try:
        started = time.monotonic()
        tokens = _TOKENIZER(list(texts)).to(_device())
        metrics.add(text_tokenize_ms=(time.monotonic() - started) * 1000)
        with torch.no_grad():
            waiting = time.monotonic()
            with _MODEL_LOCK:
                metrics.add(text_lock_wait_ms=(time.monotonic() - waiting) * 1000)
                started = time.monotonic()
                try:
                    text_features = _MODEL.encode_text(tokens)
                finally:
                    metrics.add(text_inference_ms=(time.monotonic() - started) * 1000)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        return text_features.cpu().tolist()
    except Exception:
        return []


_COMMON_WORD_EMBEDDINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'common_word_embeddings.npz')
_COMMON_WORD_EMBEDDINGS_LOCK = threading.Lock()
_COMMON_WORD_EMBEDDINGS_CACHE: Optional[Dict[str, object]] = None


def _load_common_word_embeddings() -> Dict[str, object]:
    """Static, precomputed CLIP text embeddings for a fixed common-word
    vocabulary (see scripts/generate_common_word_embeddings.py) -- a plain
    numpy file load, not a live model, so this works in the backend role too
    (which never installs torch/open_clip -- see docs/ipworker-architecture.md).
    Mirrors how Apple's on-device NLEmbedding is a shipped, precomputed word-
    vector table rather than something re-inferred per query."""
    global _COMMON_WORD_EMBEDDINGS_CACHE
    if _COMMON_WORD_EMBEDDINGS_CACHE is not None:
        return _COMMON_WORD_EMBEDDINGS_CACHE
    with _COMMON_WORD_EMBEDDINGS_LOCK:
        if _COMMON_WORD_EMBEDDINGS_CACHE is not None:
            return _COMMON_WORD_EMBEDDINGS_CACHE
        try:
            data = np.load(_COMMON_WORD_EMBEDDINGS_PATH)
            words = [str(w) for w in data['words']]
            embeddings = np.asarray(data['embeddings'], dtype=np.float32)
        except Exception:
            words, embeddings = [], np.zeros((0, 0), dtype=np.float32)
        _COMMON_WORD_EMBEDDINGS_CACHE = {
            'words': words,
            'embeddings': embeddings,
            'index': {word: i for i, word in enumerate(words)},
        }
        return _COMMON_WORD_EMBEDDINGS_CACHE


def common_word_embedding(word: str) -> List[float]:
    """Embedding for `word` from the fixed vocabulary above, or [] if it's
    outside that vocabulary -- a query word with no entry simply gets no
    semantic expansion (graceful degradation), rather than a wrong or
    incompatible-vector-space comparison."""
    cache = _load_common_word_embeddings()
    idx = cache['index'].get(str(word or '').strip())
    if idx is None:
        return []
    return cache['embeddings'][idx].tolist()


_TAG_VOCABULARY_EMBEDDINGS_PATH = os.getenv(
    'TAG_VOCABULARY_EMBEDDINGS_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'tag_vocabulary_embeddings.npz'),
)
_TAG_VOCABULARY_EMBEDDINGS_LOCK = threading.Lock()
_TAG_VOCABULARY_EMBEDDINGS_CACHE: Optional[Dict[str, object]] = None


def load_tag_vocabulary_embeddings() -> Dict[str, object]:
    """Static, precomputed CLIP text embeddings for ipwork_vision.py's ~10k
    zero-shot tagging vocabulary (see scripts/generate_tag_vocabulary_embeddings.py)
    -- the same offline-precomputed-npz pattern as
    _load_common_word_embeddings() above, for the same reason (a plain numpy
    load works in the backend role without torch/open_clip) plus a second
    one: ipworker used to re-encode this vocabulary's text tower once per
    process at startup (vision_utils.encode_text_embeddings_batch), which at
    10k labels adds real CPU cost to every cold start under this fleet's
    frequent KEDA scale-out. Loading a shipped file instead makes cold start
    cost independent of vocabulary size. Returns {'words': [...],
    'embeddings': np.ndarray} with empty values if the file is missing."""
    global _TAG_VOCABULARY_EMBEDDINGS_CACHE
    if _TAG_VOCABULARY_EMBEDDINGS_CACHE is not None:
        return _TAG_VOCABULARY_EMBEDDINGS_CACHE
    with _TAG_VOCABULARY_EMBEDDINGS_LOCK:
        if _TAG_VOCABULARY_EMBEDDINGS_CACHE is not None:
            return _TAG_VOCABULARY_EMBEDDINGS_CACHE
        try:
            data = np.load(_TAG_VOCABULARY_EMBEDDINGS_PATH)
            words = [str(w) for w in data['words']]
            embeddings = np.asarray(data['embeddings'], dtype=np.float32)
        except Exception:
            words, embeddings = [], np.zeros((0, 0), dtype=np.float32)
        _TAG_VOCABULARY_EMBEDDINGS_CACHE = {'words': words, 'embeddings': embeddings}
        return _TAG_VOCABULARY_EMBEDDINGS_CACHE
