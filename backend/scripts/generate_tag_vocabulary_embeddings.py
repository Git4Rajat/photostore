"""One-time offline generator for the CLIP tag-vocabulary embeddings shared by
both ipworker (backend/data/tag_vocabulary_embeddings.npz) and the browser
worker (frontend/public/models/browser-ai/vocab/tag-vocabulary-embeddings.v1.bin).

NOT run by the running service. Re-run only when tag-vocabulary.v1.json's
label list changes (see generate_tag_vocabulary.py) or the CLIP checkpoint
changes.

Both consumers used to compute these embeddings themselves at runtime:
ipwork_vision.py once per process via vision_utils.encode_text_embeddings_batch,
and browserAiWorker.ts on *every single photo* (transformers.js's combined
CLIP graph requires text input on every forward call). At vocabulary sizes
in the tens of thousands, the browser cost in particular stops being
tolerable, so both now load a precomputed embeddings matrix instead --
mirrors the existing backend/scripts/generate_common_word_embeddings.py
pattern, just also emitting a browser-loadable binary.

This also fixes a latent scoring mismatch: browserAiWorker.ts previously
wrapped each label in a CLIP zero-shot prompt template ("a photo of a {}")
before encoding, while ipwork_vision.py encoded bare words -- so the same
vocabulary produced two different embedding spaces depending on which
platform tagged a given photo. Generating one shared file with one template
for both fixes that as a side effect.

Usage (from backend/, with torch + open_clip installed):
    python scripts/generate_tag_vocabulary_embeddings.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import open_clip
import torch

# Matches vision_utils.py's _load_model() defaults exactly -- the checkpoint
# must be identical for these vectors to share a space with image embeddings
# computed at runtime by ipworker/browser.
MODEL_NAME = os.getenv('OPENCLIP_MODEL', 'ViT-B-32-quickgelu')
MODEL_PRETRAINED = os.getenv('OPENCLIP_PRETRAINED', 'openai')

# Same CLIP zero-shot prompt template browserAiWorker.ts used to apply
# per-photo (ZERO_SHOT_HYPOTHESIS_TEMPLATE); now baked into the precomputed
# vectors once instead.
HYPOTHESIS_TEMPLATE = 'a photo of a {}'

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOCAB_JSON_PATH = os.path.join(_ROOT, '..', 'frontend', 'public', 'models', 'browser-ai', 'vocab', 'tag-vocabulary.v1.json')
NPZ_OUTPUT_PATH = os.path.join(_ROOT, 'data', 'tag_vocabulary_embeddings.npz')
BIN_OUTPUT_PATH = os.path.join(_ROOT, '..', 'frontend', 'public', 'models', 'browser-ai', 'vocab', 'tag-vocabulary-embeddings.v1.bin')

BATCH_SIZE = 512


def main() -> None:
    with open(VOCAB_JSON_PATH, 'r', encoding='utf-8') as handle:
        vocab_payload = json.load(handle)
    labels = [str(label) for label in (vocab_payload.get('labels') or []) if str(label or '').strip()]
    if not labels:
        raise SystemExit(f'No labels found in {VOCAB_JSON_PATH}')

    print(f'Encoding {len(labels)} labels with {MODEL_NAME}/{MODEL_PRETRAINED} (template: "{HYPOTHESIS_TEMPLATE}")...')

    model, _, _ = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=MODEL_PRETRAINED)
    model.eval()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)

    texts = [HYPOTHESIS_TEMPLATE.format(label) for label in labels]
    all_embeddings = []
    with torch.no_grad():
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start:start + BATCH_SIZE]
            tokens = tokenizer(batch)
            features = model.encode_text(tokens)
            features = features / features.norm(dim=-1, keepdim=True)
            all_embeddings.append(features.cpu().numpy().astype(np.float32))
            print(f'  {min(start + BATCH_SIZE, len(texts))}/{len(texts)}')

    embeddings = np.concatenate(all_embeddings, axis=0)
    assert embeddings.shape == (len(labels), embeddings.shape[1])

    os.makedirs(os.path.dirname(NPZ_OUTPUT_PATH), exist_ok=True)
    np.savez_compressed(
        NPZ_OUTPUT_PATH,
        words=np.asarray(labels),
        embeddings=embeddings,
        model_name=np.asarray([MODEL_NAME]),
        model_pretrained=np.asarray([MODEL_PRETRAINED]),
        hypothesis_template=np.asarray([HYPOTHESIS_TEMPLATE]),
    )
    print(f'Wrote {len(labels)}x{embeddings.shape[1]} embeddings to {NPZ_OUTPUT_PATH}')

    # Browser side: a flat row-major float32 binary, same label order as the
    # vocabulary JSON, so the worker can zip label[i] <-> embeddings[i] by
    # index without needing to parse a second JSON/npz format in JS.
    flat_bytes = np.ascontiguousarray(embeddings, dtype='<f4').tobytes()
    os.makedirs(os.path.dirname(BIN_OUTPUT_PATH), exist_ok=True)
    with open(BIN_OUTPUT_PATH, 'wb') as handle:
        handle.write(flat_bytes)
    sha256 = hashlib.sha256(flat_bytes).hexdigest()
    print(f'Wrote {len(flat_bytes)} bytes to {BIN_OUTPUT_PATH}')
    print(f'  bytes={len(flat_bytes)}')
    print(f'  sha256={sha256}')
    print('Register these in frontend/public/models/browser-ai/manifest.json assets[].')


if __name__ == '__main__':
    main()
