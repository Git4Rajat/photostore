import io
import os
import re
import unicodedata
from typing import List

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

try:
    import torch
    import open_clip
    from PIL import Image, ImageOps
except Exception:
    torch = None
    open_clip = None

_MODEL = None
_TOKENIZER = None
_PREPROCESS = None
_MODEL_NAME = ''
_MODEL_PRETRAINED = ''

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
    if not text:
        return []
    if not _load_model():
        return _hash_text_embedding(text)
    try:
        tokens = _TOKENIZER([text]).to(_device())
        with torch.no_grad():
            text_features = _MODEL.encode_text(tokens)
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
            image = ImageOps.exif_transpose(image)
            pixel_values = _PREPROCESS(image.convert('RGB')).unsqueeze(0).to(_device())
        with torch.no_grad():
            image_features = _MODEL.encode_image(pixel_values)
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        return image_features.squeeze(0).cpu().tolist()
    except Exception:
        return []


# Precomputed once per process and cached by ipwork_vision.py -- encoding the
# vocabulary's ~500 labels through the text tower on every single photo would
# be far more wasteful than once at process startup, since the vocabulary
# itself never changes at runtime.
def encode_text_embeddings_batch(texts: List[str]) -> List[List[float]]:
    if not texts or not _load_model():
        return []
    try:
        tokens = _TOKENIZER(list(texts)).to(_device())
        with torch.no_grad():
            text_features = _MODEL.encode_text(tokens)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        return text_features.cpu().tolist()
    except Exception:
        return []


def get_logit_scale() -> float:
    """CLIP's learned temperature (logit_scale.exp()), applied before the
    softmax in zero-shot classification -- the same scaling transformers.js's
    exported ONNX graph bakes into `logits_per_image`
    (frontend/src/workers/browserAiWorker.ts's `classify`), so ipworker's
    from-scratch computation reproduces the same calibration instead of an
    unscaled cosine-similarity softmax."""
    if not _load_model():
        return 100.0
    try:
        return float(_MODEL.logit_scale.exp().item())
    except Exception:
        return 100.0
