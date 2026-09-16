"""Generates a real .npz vector-index fixture, byte-for-byte matching
storage_utils._serialize_vector_index's actual output, for round-trip
testing the TypeScript ZIP+NPY parser (frontend/src/services/
localVectorIndexParser.ts) that reads this same blob format client-side for
Phase B (client-side semantic search) -- see backend-cpu-optimization-2026-09
memory.

This is deliberately NOT imported from storage_utils (that function needs a
live Azure context for the surrounding class); it's a direct copy of the
serialization logic, kept in sync by comment reference. If
_serialize_vector_index's array set/dtypes ever change, regenerate this and
update the TS parser to match.
"""
import io
import json
import os

import numpy as np

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tests', 'fixtures')

ROW_KEYS = ['dog1.jpg', 'sunset_beach.jpg', 'malmo_trip.cr3', 'unicode_café_日本.jpg']
SOURCE_VERSION = '2026-09-15T12:00:00+00:00'
EMBEDDING_VERSION = 'clip-vit-base-patch32:openai:browser-v1'
UPDATED_AT = '2026-09-15T12:00:01+00:00'

# Deterministic, non-trivial (not all-zero/all-same) 512-d embeddings so a
# parser bug (wrong stride, wrong byte order, transposed shape) shows up as a
# real numeric mismatch rather than accidentally passing on degenerate data.
rng = np.random.default_rng(42)
EMBEDDINGS = rng.normal(size=(len(ROW_KEYS), 512)).astype(np.float32)
EMBEDDINGS = EMBEDDINGS / np.linalg.norm(EMBEDDINGS, axis=1, keepdims=True)


def serialize() -> bytes:
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        embeddings=np.asarray(EMBEDDINGS, dtype=np.float32),
        row_keys=np.asarray([json.dumps(ROW_KEYS, ensure_ascii=False, separators=(',', ':'))]),
        source_version=np.asarray([SOURCE_VERSION]),
        embedding_version=np.asarray([EMBEDDING_VERSION]),
        updated_at=np.asarray([UPDATED_AT]),
    )
    return buffer.getvalue()


if __name__ == '__main__':
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    payload = serialize()
    npz_path = os.path.join(FIXTURE_DIR, 'vector_index_parity.npz')
    with open(npz_path, 'wb') as f:
        f.write(payload)

    # Sidecar JSON with the expected values in a JS-friendly form, so the TS
    # test doesn't need its own numpy-reading step to know what "correct"
    # looks like -- it just compares the parser's output against this.
    expected = {
        'rowKeys': ROW_KEYS,
        'sourceVersion': SOURCE_VERSION,
        'embeddingVersion': EMBEDDING_VERSION,
        'updatedAt': UPDATED_AT,
        'embeddingsShape': list(EMBEDDINGS.shape),
        'embeddingsFlat': EMBEDDINGS.flatten().tolist(),
    }
    json_path = os.path.join(FIXTURE_DIR, 'vector_index_parity.expected.json')
    with open(json_path, 'w') as f:
        json.dump(expected, f)

    # Round-trip sanity check via numpy itself before trusting this fixture.
    with np.load(io.BytesIO(payload), allow_pickle=False) as data:
        assert np.allclose(np.asarray(data['embeddings'], dtype=np.float32), EMBEDDINGS)
        assert json.loads(str(data['row_keys'][0])) == ROW_KEYS
    print(f'Wrote {npz_path} ({len(payload)} bytes) and {json_path}')
