"""Reusable FAISS face-index builder and offline candidate comparison helpers.

Builds and queries FAISS indexes over existing face embeddings. This module
does not write any person/face/metadata table row. The offline comparison
script measures candidate recall against the linear assignment scan, while
clustering_runtime uses the same builder for live candidate retrieval.

Deliberately has no dependency on app.py/storage_utils.py globals: every
function here takes plain data (lists of dicts, vectors) and returns plain
data, so it's unit-testable with synthetic vectors without mocking Table
Storage or Flask. LiveFaceIndex adds disk-exact reranking and a bounded mutable
delta. The Table Storage / Blob Storage wiring for offline comparison lives
in the comparison script, following the
existing backend/scripts/*.py pattern of `import app` for storage access.

Indexes are homogeneous in alignment tier, embedding version and dimension;
the caller supplies library/user isolation. They are
never mixed across tiers, matching the existing DBSCAN repair path's tier
separation (app.py's PEOPLE_CLUSTER_ALIGNMENT_TIERS / _face_alignment_tier):
a same-person cross-tier similarity score is statistically indistinguishable
from noise, so an index blending tiers would produce meaningless candidates
regardless of what recall it measured.
"""
from __future__ import annotations

import json
import tempfile
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Iterable, Callable

import numpy as np

try:
    import faiss
except ImportError:  # pragma: no cover - exercised only when faiss-cpu isn't installed
    faiss = None

# Below this many vectors, use an exact brute-force index (IndexFlatIP) --
# IVF needs enough training data to form meaningful Voronoi cells, and exact
# search is cheap enough at this scale anyway. Above it, use IndexIVFPQ.
# These are starting points to benchmark, not tuned production settings --
# see the design doc's "trial coarse-list counts around 4K-8K for a million-
# vector collection" guidance.
FLAT_INDEX_MAX_VECTORS = 10_000
IVFPQ_NLIST_MIN = 4096
IVFPQ_NLIST_MAX = 8192
IVFPQ_PQ_SUBQUANTIZERS = 64  # one byte per subquantizer -> 64-byte PQ codes
IVFPQ_PQ_BITS = 8
IVFPQ_NPROBE_DEFAULT = 16


@dataclass(frozen=True)
class IndexConfig:
    flat_max_vectors: int = FLAT_INDEX_MAX_VECTORS
    nlist_min: int = IVFPQ_NLIST_MIN
    nlist_max: int = IVFPQ_NLIST_MAX
    pq_subquantizers: int = IVFPQ_PQ_SUBQUANTIZERS
    pq_bits: int = IVFPQ_PQ_BITS
    training_sample_size: int = 100_000
    training_points_per_centroid: int = 39
    training_iterations: int = 25
    seed: int = 0
    # Conservative accounted working-set ceiling, not a process RSS guarantee.
    memory_budget_bytes: int = 3 * 1024 ** 3


def normalized_vector(value):
    """Parse inline JSON or arrays; invalid/nonfinite/zero vectors are skipped."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        vector = np.asarray(value, dtype='float32')
        if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
            return None
        norm = float(np.linalg.norm(vector.astype('float64')))
        if not np.isfinite(norm) or norm <= 0:
            return None
        return np.ascontiguousarray(vector / norm, dtype='float32')
    except (ValueError, TypeError, OverflowError):
        return None


def _require_faiss() -> None:
    if faiss is None:
        raise RuntimeError(
            "faiss-cpu is not installed. Install backend/requirements-clustering.txt "
            "to use clustering_index.py -- it's deliberately kept out of the main "
            "backend/ipworker images (see that file's comment)."
        )


@dataclass
class FaceIndexBuildResult:
    index: object  # faiss.Index -- left untyped so this module still imports without faiss present
    face_ids: List[str]  # face_ids[i] is the face_id for vector row i in the index
    dimension: int
    tier: str
    embedding_version: str
    vector_count: int
    index_type: str  # 'flat' or 'ivfpq'
    built_at: str


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _new_index(dimension: int, vector_count: int, *, training_count=None, config=None):
    """L2-normalized vectors + inner product = cosine similarity, matching
    the rest of the clustering code's cosine-based thresholds -- no separate
    distance-to-similarity conversion needed at query time."""
    _require_faiss()
    config = config or IndexConfig()
    if vector_count <= config.flat_max_vectors:
        return faiss.IndexFlatIP(dimension), 'flat'
    training_count = min(vector_count, config.training_sample_size) if training_count is None else training_count
    if training_count < config.training_points_per_centroid * (1 << config.pq_bits):
        raise ValueError('Insufficient training sample for PQ codebook')
    desired = max(config.nlist_min, min(config.nlist_max, int(vector_count ** 0.5) * 4))
    nlist = min(desired, max(1, training_count // config.training_points_per_centroid))
    quantizer = faiss.IndexFlatIP(dimension)
    # PQ requires dimension to be an exact multiple of the subquantizer count
    # (faiss raises an opaque C++ error otherwise) -- production's 512-d
    # ArcFace/AdaFace embeddings divide evenly by 64, but fall back to the
    # largest compatible divisor rather than crashing for any other
    # embedding space this module might see in the future.
    subquantizers = min(dimension, config.pq_subquantizers)
    while dimension % subquantizers != 0:
        subquantizers -= 1
    index = faiss.IndexIVFPQ(
        quantizer, dimension, nlist, subquantizers, config.pq_bits, faiss.METRIC_INNER_PRODUCT,
    )
    # Precomputed tables can cost roughly 256 MiB per index at 4K lists / 64
    # subquantizers (see design doc's memory-budget correction) -- explicitly
    # disabled until a future pass benchmarks enabling them against the real
    # container memory budget.
    index.use_precomputed_table = -1
    index.cp.niter = config.training_iterations
    index.cp.seed = config.seed
    index.pq.cp.niter = config.training_iterations
    index.pq.cp.seed = config.seed
    return index, 'ivfpq'


def build_face_index(
    tier: str,
    embedding_rows: Iterable[Dict] | Callable[[], Iterable[Dict]],
    *,
    batch_size: int = 2000,
    vector_count: Optional[int] = None,
    config: Optional[IndexConfig] = None,
) -> Optional[FaceIndexBuildResult]:
    """Single-pass iterable/factory build, with bounded float32 disk spool.

    Reservoir sampling covers the entire input deterministically, not its
    first batch. Only IDs, a capped training reservoir and an add batch stay
    in RAM (plus FAISS). Temporary disk needs count * dimension * 4 bytes.
    vector_count, if supplied, counts input rows, including skipped invalid
    vectors. Dimension/version/tier mismatches fail closed; invalid vectors
    are skipped. Caller-owned lists/caches are outside our memory accounting.
    """
    _require_faiss()
    config = config or IndexConfig()
    if (batch_size <= 0 or config.training_sample_size <= 0 or
            config.training_points_per_centroid <= 0 or config.training_iterations <= 0 or
            not 1 <= config.pq_bits <= 8 or config.pq_subquantizers <= 0 or
            config.nlist_min <= 0 or config.nlist_max < config.nlist_min or
            config.flat_max_vectors < 0 or not 0 < config.memory_budget_bytes < 4 * 1024 ** 3 or
            (vector_count is not None and (type(vector_count) is not int or vector_count < 0))):
        raise ValueError('Invalid index configuration/count')
    face_ids: List[str] = []
    unique_ids = set()
    dimension = None
    embedding_version = ''
    rng = random.Random(config.seed)
    seen = 0
    id_bytes = 0
    with tempfile.TemporaryFile() as spool:
        for row in (embedding_rows() if callable(embedding_rows) else embedding_rows):
            seen += 1
            vector = normalized_vector(row.get('embedding'))
            if vector is None:
                continue
            version = str(row.get('embeddingVersion') or '')
            if row.get('tier', tier) != tier:
                raise ValueError('Mixed alignment tiers')
            if dimension is None:
                dimension, embedding_version = len(vector), version
                # Allow headroom for FAISS training copies/workspaces and IDs.
                capacity = min(config.training_sample_size,
                               config.memory_budget_bytes // (dimension * 4 * 32))
                if capacity < 1 or batch_size * dimension * 4 * 4 > config.memory_budget_bytes // 4:
                    raise ValueError('Training/batch exceeds memory budget')
                # Grow with actual input: tiny or exhausted generators should
                # not reserve a production-sized reservoir unnecessarily.
                sample = np.empty((min(capacity, batch_size), dimension), dtype='float32')
            if len(vector) != dimension or version != embedding_version:
                raise ValueError('Mixed embedding dimensions or versions')
            face_id = str(row.get('faceId') or '')
            if not face_id:
                raise ValueError('Missing faceId')
            if face_id in unique_ids:
                raise ValueError('Duplicate faceId')
            unique_ids.add(face_id)
            face_ids.append(face_id)
            id_bytes += sys.getsizeof(face_id)
            count = len(face_ids)
            if count <= capacity and count > len(sample):
                grown = np.empty((min(capacity, max(count, len(sample) * 2)), dimension), dtype='float32')
                grown[:len(sample)] = sample
                sample = grown
            # Worst-case flat storage until selection; IVF codes thereafter.
            bytes_per_vector = dimension * 4 if count <= config.flat_max_vectors else config.pq_subquantizers + 8
            estimated = (id_bytes + count * (bytes_per_vector * 2 + 256) + sample.nbytes * 16 +
                         batch_size * dimension * 4 * 4 + config.nlist_max * dimension * 4 * 4 +
                         (1 << config.pq_bits) * dimension * 4 * 4)
            if estimated > config.memory_budget_bytes:
                raise ValueError('Estimated index working set exceeds memory budget')
            spool.write(vector.tobytes())
            slot = count - 1 if count <= capacity else rng.randrange(count)
            if slot < capacity:
                sample[slot] = vector
        if vector_count is not None and seen != vector_count:
            raise ValueError('Input row count does not match vector_count')
        if not face_ids:
            return None
        training_count = min(len(face_ids), capacity)
        index, index_type = _new_index(dimension, len(face_ids), training_count=training_count, config=config)
        if index_type == 'ivfpq':
            index.train(sample[:training_count])
        del sample
        spool.seek(0)
        while data := spool.read(batch_size * dimension * 4):
            index.add(np.frombuffer(data, dtype='float32').reshape(-1, dimension))

    if index_type == 'ivfpq':
        index.nprobe = min(index.nlist, IVFPQ_NPROBE_DEFAULT)

    return FaceIndexBuildResult(
        index=index,
        face_ids=face_ids,
        dimension=dimension,
        tier=tier,
        embedding_version=embedding_version,
        vector_count=len(face_ids),
        index_type=index_type,
        built_at=datetime.now(timezone.utc).isoformat(),
    )


def search_candidates(
    build: FaceIndexBuildResult, query_vector: List[float], k: int = 10,
) -> List[Tuple[str, float]]:
    """Up to k (face_id, cosine_similarity) candidates for one query vector,
    nearest-first. Exact for 'flat' indexes; approximate (IVFPQ) for large
    ones. Candidate retrieval only -- never sufficient evidence for a merge
    decision by itself (exact reranking and dedup-by-person still belong to
    the caller, per the design doc's accuracy-protection rules)."""
    if build is None or build.index.ntotal == 0:
        return []
    vector = normalized_vector(query_vector)
    if vector is None or len(vector) != build.dimension or k <= 0:
        return []
    query = vector.reshape(1, -1)
    k = min(k, build.index.ntotal)
    scores, indices = build.index.search(query, k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        results.append((build.face_ids[int(idx)], float(score)))
    return results


def serialize_index(build: FaceIndexBuildResult) -> Tuple[bytes, Dict]:
    """Split a build into (raw FAISS index bytes, JSON-able manifest) for a
    checkpoint -- face_ids/dimension/tier/etc. don't belong inside the FAISS
    blob itself, so a reader can inspect/validate the manifest without
    deserializing the (potentially large) index first."""
    _require_faiss()
    index_bytes = faiss.serialize_index(build.index).tobytes()
    manifest = {
        'faceIds': build.face_ids,
        'dimension': build.dimension,
        'tier': build.tier,
        'embeddingVersion': build.embedding_version,
        'vectorCount': build.vector_count,
        'indexType': build.index_type,
        'builtAt': build.built_at,
    }
    return index_bytes, manifest


def deserialize_index(index_bytes: bytes, manifest: Dict) -> FaceIndexBuildResult:
    _require_faiss()
    if not isinstance(manifest, dict):
        raise ValueError('Invalid index manifest')
    ids = manifest.get('faceIds')
    count, dimension = manifest.get('vectorCount'), manifest.get('dimension')
    kind = manifest.get('indexType')
    if (type(count) is not int or count < 0 or type(dimension) is not int or dimension <= 0 or
            not isinstance(ids, list) or len(ids) != count or
            any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != count or
            kind not in ('flat', 'ivfpq') or
            any(not isinstance(manifest.get(key), str) for key in ('tier', 'embeddingVersion', 'builtAt')) or
            not manifest['tier']):
        raise ValueError('Invalid index manifest')
    try:
        index = faiss.deserialize_index(np.frombuffer(index_bytes, dtype='uint8'))
    except (RuntimeError, ValueError, TypeError) as exc:
        raise ValueError('Invalid serialized FAISS index') from exc
    expected_class = faiss.IndexFlatIP if kind == 'flat' else faiss.IndexIVFPQ
    if (not isinstance(index, expected_class) or index.d != dimension or index.ntotal != count or
            not index.is_trained or index.metric_type != faiss.METRIC_INNER_PRODUCT):
        raise ValueError('Index does not match manifest')
    if kind == 'ivfpq':
        index.use_precomputed_table = -1
        index.nprobe = min(index.nlist, IVFPQ_NPROBE_DEFAULT)
    return FaceIndexBuildResult(
        index=index,
        face_ids=list(manifest.get('faceIds') or []),
        dimension=int(manifest.get('dimension') or 0),
        tier=str(manifest.get('tier') or ''),
        embedding_version=str(manifest.get('embeddingVersion') or ''),
        vector_count=int(manifest.get('vectorCount') or 0),
        index_type=str(manifest.get('indexType') or ''),
        built_at=str(manifest.get('builtAt') or ''),
    )
