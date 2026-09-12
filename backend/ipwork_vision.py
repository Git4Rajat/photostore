"""ipworker's vision (CLIP tagging + image embedding) step processor.

Ports the browser's transformers.js CLIP zero-shot classification
(frontend/src/workers/browserAiWorker.ts) to Python via open_clip, reusing
vision_utils.py's already-CLIP-compatible model loading (it already has to
match the browser's checkpoint for text-query embeddings to share the same
vector space -- see vision_utils.py's comments on ViT-B-32-quickgelu).

Deliberately narrower than the browser worker: this ports the core zero-shot
tagging + image embedding, not every secondary heuristic (e.g.
inferClothingColorTags' pixel-color-histogram clothing tags are not ported --
a smaller, separable enhancement, not core vision tagging).

torch/open_clip are heavy deps that live only in the ipworker image's
requirements (backend/requirements.txt intentionally does NOT have them, so
the plain backend/worker roles keep falling back to the lightweight hashing
text-embedding path) -- this module is imported lazily, only from
app.run_ipworker().
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import numpy as np

import vision_utils
from image_utils import RAW_EXTENSIONS_CINEMA, RAW_EXTENSIONS_RAWPY, extract_raw_preview_bytes
from search_utils import PERSON_SCORE_THRESHOLD

# Same vocabulary file the browser fetches at runtime (manifest.tagVocabularyUrl,
# default frontend/public/models/browser-ai/vocab/tag-vocabulary.v1.json) --
# bundled into the ipworker image at build time (see backend/ipworker.Dockerfile).
VOCAB_PATH = os.getenv(
    'IPWORKER_VOCAB_PATH',
    os.path.join(os.getenv('IPWORKER_MODELS_DIR', '/app/models'), '..', 'vocab', 'tag-vocabulary.v1.json'),
)

# frontend/src/workers/browserAiWorker.ts PERSON_LABELS / DEFAULT_VOCAB_TOP_K.
PERSON_LABELS = {
    'person', 'people', 'portrait', 'human', 'face', 'selfie', 'man', 'woman',
    'boy', 'girl', 'child', 'baby', 'toddler', 'adult', 'group', 'family', 'crowd',
}
DEFAULT_VOCAB_TOP_K = 15

_vocab_labels: Optional[List[str]] = None
_vocab_version = ''
_vocab_embeddings: Optional[np.ndarray] = None  # len(labels) x dim, unit-normalized


def _load_vocabulary() -> bool:
    global _vocab_labels, _vocab_version, _vocab_embeddings
    if _vocab_labels is not None and _vocab_embeddings is not None:
        return True
    try:
        with open(VOCAB_PATH, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        labels = [str(label) for label in (payload.get('labels') or []) if str(label or '').strip()]
        if not labels:
            return False
    except Exception:
        return False

    # Precomputed via scripts/generate_tag_vocabulary_embeddings.py (shared
    # with the browser worker, same CLIP checkpoint + prompt template) --
    # avoids re-encoding ~10k labels' text tower on every ipworker cold
    # start, which under this fleet's frequent KEDA scale-out would add real
    # CPU cost cluster-wide. Falls back to live encoding (the old behavior)
    # if the shipped file is missing or out of sync with the vocabulary JSON
    # (e.g. one was regenerated without the other) rather than failing hard.
    precomputed = vision_utils.load_tag_vocabulary_embeddings()
    if precomputed['words'] == labels and len(precomputed['words']) > 0:
        embeddings = precomputed['embeddings']
    else:
        embeddings = vision_utils.encode_text_embeddings_batch(labels)
        if not embeddings or len(embeddings) != len(labels):
            return False

    _vocab_labels = labels
    _vocab_version = str(payload.get('version') or '')
    _vocab_embeddings = np.asarray(embeddings, dtype=np.float32)
    return True


def _decodable_image_bytes(image_bytes: bytes, filename: str) -> bytes:
    """See ipwork_face.py's copy of this helper: RAW formats (e.g. CR3) have
    no generic PIL codec, so vision_utils.encode_image_embedding's Image.open()
    raises on the raw bytes unless it's handed the same embedded/rawpy preview
    ipwork_thumbnail.py already extracts."""
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return preview
    return image_bytes


def process_vision(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    if not vision_utils.image_encoder_available():
        return {'hasData': False, 'error': 'clip_model_unavailable'}
    if not _load_vocabulary():
        return {'hasData': False, 'error': 'vocabulary_unavailable'}

    image_embedding = vision_utils.encode_image_embedding(_decodable_image_bytes(image_bytes, filename))
    if not image_embedding:
        return {'hasData': False, 'error': 'image_embedding_failed'}

    image_vec = np.asarray(image_embedding, dtype=np.float32)
    # Raw cosine similarity per label (both sides L2-normalized), scored
    # independently rather than via a softmax over the whole vocabulary --
    # softmax normalizes probability mass across every candidate label, so
    # a correct match's score shrinks as the vocabulary grows (this is what
    # let a small foreground subject like a jet lose to a dominant "sky"
    # background even when "airplane" was a real candidate). Cosine
    # similarity stays on a fixed, vocab-size-independent scale instead.
    # See browserAiWorker.ts's toPredictions for the matching browser-side
    # change.
    scores = _vocab_embeddings @ image_vec
    top_k = min(DEFAULT_VOCAB_TOP_K, len(_vocab_labels))
    top_indices = np.argsort(-scores)[:top_k]
    predictions = [{'label': _vocab_labels[i], 'score': float(max(0.0, scores[i]))} for i in top_indices]
    tags = [p['label'] for p in predictions]

    best_person = next((p for p in predictions if p['label'] in PERSON_LABELS), None)
    ai_person_candidate = bool(best_person and best_person['score'] >= PERSON_SCORE_THRESHOLD)

    return {
        'hasData': True,
        'tags': tags,
        'objects': tags,
        'caption': '',
        'predictions': predictions,
        'imageEmbedding': image_embedding,
        'aiPersonCandidate': ai_person_candidate,
        'aiPersonLabel': best_person['label'] if best_person else '',
        'aiPersonScore': best_person['score'] if best_person else 0.0,
        'modelAvailability': 'available',
        'model': 'open_clip ViT-B-32-quickgelu',
        'modelVersion': vision_utils.get_text_embedding_version(),
        'modelTaxonomyVersion': f'clip-vocab:{_vocab_version or "unknown"}',
        'runtime': 'open_clip+torch',
    }
