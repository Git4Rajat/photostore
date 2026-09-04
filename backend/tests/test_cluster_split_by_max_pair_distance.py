"""Regression test for a live incident found 2026-09-04: a worker's CPU sat
pegged at 100% for 7+ hours processing a single clustering job, invisible to
the 15-minute stale-job sweep because the same day's heartbeat fix (see
test_clustering_job_heartbeat.py) kept refreshing updatedAt regardless of
whether real progress was happening.

Root cause: _split_cluster_by_max_pair_distance recomputed every pairwise
distance in the growing candidate cluster from scratch on every candidate
check, making it roughly O(cluster_size^3). Fine at the few hundred faces
this was calibrated against; an effective hang once one person's DBSCAN
cluster grew into the low thousands (this account had ~15k faces total).

The fix replaces the from-scratch pairwise recomputation with incrementally
maintained running distances -- see the function's comments for why that's
equivalent, not just faster.
"""
from __future__ import annotations

import itertools
import random
import time

import numpy as np
import pytest

import app


def _reference_split_cluster_by_max_pair_distance(indices, dist_matrix, max_distance):
    """Deliberately un-optimized transliteration of the pre-fix algorithm,
    kept only as a small-input correctness oracle -- this is what the fast
    version must keep matching exactly, not just "produce some valid split."
    """
    if len(indices) <= 1:
        return [list(indices)]

    threshold = max(0.0, float(max_distance))
    remaining = list(indices)
    split_clusters = []

    while remaining:
        seed = min(
            remaining,
            key=lambda idx: (
                sum(float(dist_matrix[idx, other]) for other in remaining if other != idx),
                idx,
            ),
        )
        cluster = [seed]
        remaining.remove(seed)

        while remaining:
            candidates = []
            for idx in remaining:
                candidate_cluster = [*cluster, idx]
                max_pair_distance = max(
                    float(dist_matrix[left, right])
                    for pos, left in enumerate(candidate_cluster)
                    for right in candidate_cluster[pos + 1:]
                )
                if max_pair_distance <= threshold:
                    distances_to_cluster = [float(dist_matrix[idx, member]) for member in cluster]
                    candidates.append((max_pair_distance, sum(distances_to_cluster), idx))
            if not candidates:
                break
            _, _, next_idx = min(candidates)
            cluster.append(next_idx)
            remaining.remove(next_idx)

        split_clusters.append(sorted(cluster))

    return split_clusters


def _random_dist_matrix(rng, n, scale):
    pts = rng.rand(n, 3) * scale
    return np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)


def test_matches_reference_on_random_small_inputs():
    rng = np.random.RandomState(1234)
    py_random = random.Random(1234)
    for _ in range(300):
        n = py_random.randint(1, 14)
        scale = py_random.choice([0.3, 1.0, 2.0])
        dm = _random_dist_matrix(rng, n, scale)
        threshold = py_random.choice([0.1, 0.3, 0.5, 0.8, 1.0, 1.5])
        indices = list(range(n))

        expected = _reference_split_cluster_by_max_pair_distance(indices, dm, threshold)
        actual = app._split_cluster_by_max_pair_distance(indices, dm, threshold)
        assert actual == expected, f"n={n} threshold={threshold}\n{dm}"


def test_result_is_a_valid_partition_and_respects_the_diameter_bound():
    rng = np.random.RandomState(99)
    for n in (1, 2, 5, 40, 150):
        dm = _random_dist_matrix(rng, n, 1.0)
        threshold = 0.6
        indices = list(range(n))
        result = app._split_cluster_by_max_pair_distance(indices, dm, threshold)

        flattened = sorted(itertools.chain.from_iterable(result))
        assert flattened == indices, "every index must appear exactly once"

        for members in result:
            if len(members) > 1:
                sub = dm[np.ix_(members, members)]
                assert sub.max() <= threshold + 1e-9


def test_large_single_cluster_completes_in_seconds_not_hours():
    # A single oversized DBSCAN cluster is exactly the shape that hung live:
    # every point within a loose threshold of every other, so the whole
    # thing lands in one growing candidate_cluster. The pre-fix algorithm's
    # measured scaling (0.4s @ 100 points, 6s @ 200, 30s @ 300 -- all on the
    # same hardware class as this test) extrapolates to well over an hour at
    # 1500, and to multiple hours at the ~15k-face scale that actually hung
    # in production.
    rng = np.random.RandomState(7)
    n = 1500
    dm = _random_dist_matrix(rng, n, 0.5)
    indices = list(range(n))

    start = time.time()
    result = app._split_cluster_by_max_pair_distance(indices, dm, 0.9)
    elapsed = time.time() - start

    assert elapsed < 5.0, f"took {elapsed:.1f}s -- the O(n^3) regression is back"
    flattened = sorted(itertools.chain.from_iterable(result))
    assert flattened == indices
    for members in result:
        if len(members) > 1:
            sub = dm[np.ix_(members, members)]
            assert sub.max() <= 0.9 + 1e-9
