#!/usr/bin/env python3
"""Read-only comparison: FAISS vs. compatible per-tier exact cosine search.

_assign_faces_to_people_incrementally (app.py) matches an incoming face
against every existing person's representative embedding via
_best_two_person_matches -- a full O(num_people) scan per face. This script
builds a per-alignment-tier FAISS index over stored person representatives
(clustering_index.py, shadow mode only) and, for a sample of real faces in a
real library, compares FAISS's top candidates against a compatible exact
baseline, NOT the live all-tier scan. It makes no writes or cache refreshes
and never feeds its output back into any
assignment decision -- see clustering_index.py's module docstring for why
this stays shadow mode until a future phase.

Usage:
    python scripts/compare_faiss_candidates.py <user_id>
    python scripts/compare_faiss_candidates.py <user_id> --sample 200 --k 10
"""
import argparse
import json
import os
import random
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clustering_index as ci  # noqa: E402

# Lazy import: importing the comparison helpers must not initialize storage.
app = None


def _get_app():
    global app
    if app is None:
        import app as application
        app = application
    return app


def _person_rep_rows_by_tier(user_id):
    """Stream stored reps, skipping mixed/unknown member compatibility.

    No _load_people_embedding_index: that loader materializes Python vectors
    and can refresh its durable cache. Existing face-summary caller still
    materializes the full metadata partition; this is NOT end-to-end bounded.
    A stored representative has no reliable provenance when members span
    tiers/versions, so conservatively exclude it rather than mislabel it.
    """
    _get_app()
    face_summary = app._load_user_face_summary_by_id(user_id)
    if app.person_table_client is None:
        return
    skipped = 0
    for entry in app.person_table_client.query_entities(
            f"PartitionKey eq '{app._escape_odata(user_id)}'",
            select=['RowKey', 'faceIds', 'repEmbedding']):
        person_id = str(entry.get('RowKey') or '')
        rep = ci.normalized_vector(entry.get('repEmbedding'))
        if not person_id or rep is None:
            continue
        try:
            face_ids = entry.get('faceIds') or []
            if isinstance(face_ids, str):
                face_ids = json.loads(face_ids)
            if not isinstance(face_ids, list) or not face_ids:
                raise ValueError('missing members')
        except (ValueError, TypeError):
            skipped += 1
            continue
        compatibility = set()
        unknown = False
        for face_id in face_ids:
            face = face_summary.get(str(face_id))
            if (face is None or not app._face_is_clusterable(face) or
                    not app._face_embedding_allowed_for_clustering(face)):
                unknown = True
                break
            compatibility.add((app._face_alignment_tier(face), app._face_embedding_version(face)))
        if unknown or len(compatibility) != 1:
            skipped += 1
            continue
        tier, version = next(iter(compatibility))
        yield (tier, len(rep), version), {
            'faceId': person_id, 'embedding': rep.tolist(), 'embeddingVersion': version,
        }
    print(f'Skipped {skipped} representatives with mixed/unknown member compatibility.')


def _build_comparison_indexes(rows, config=None):
    """Disk buckets avoid full raw Python lists; exact baseline retains floats."""
    config = config or ci.IndexConfig()
    with ExitStack() as stack:
        spools = {}
        for key, row in rows:
            if key not in spools:
                spools[key] = stack.enter_context(tempfile.TemporaryFile(mode='w+t'))
            spools[key].write(json.dumps(row) + '\n')
        builds = {}
        for key, spool in spools.items():
            def factory():
                spool.seek(0)
                for line in spool:
                    yield json.loads(line)
            approximate = ci.build_face_index(key[0], factory, config=config)
            exact = ci.build_face_index(key[0], factory, config=replace(
                config, flat_max_vectors=sys.maxsize, training_sample_size=1))
            if approximate is not None:
                builds[key] = (approximate, exact)
        return builds


def _sample_query_faces(user_id, sample_size, rng):
    _get_app()
    face_summary = app._load_user_face_summary_by_id(user_id)
    candidates = []
    seen = 0
    for face_id, row in face_summary.items():
        if not (app._face_is_clusterable(row) and app._face_embedding_allowed_for_clustering(row)):
            continue
        seen += 1
        slot = seen - 1 if seen <= sample_size else rng.randrange(seen)
        if slot < sample_size:
            if seen <= sample_size:
                candidates.append((face_id, row))
            else:
                candidates[slot] = (face_id, row)
    return candidates


def _decision(matches, threshold, margin):
    # Sample score/margin decision only; not the full live assignment policy.
    if not matches or matches[0][1] <= 0:
        return None
    second = max(0.0, matches[1][1]) if len(matches) > 1 else 0.0
    return matches[0][0] if matches[0][1] >= threshold and matches[0][1] - second >= margin else None


def _compare_query(approximate, exact, vector, k, threshold, margin):
    baseline = ci.search_candidates(exact, vector, k=2)
    candidates = ci.search_candidates(approximate, vector, k=k)
    candidate_ids = {pid for pid, _ in candidates}
    # Exact rerank only retrieved candidates, using the identical flat vectors.
    positions = {pid: i for i, pid in enumerate(exact.face_ids) if pid in candidate_ids}
    reranked = sorted(((pid, float(exact.index.reconstruct(positions[pid]) @ vector))
                       for pid in candidate_ids), key=lambda item: (-item[1], positions[item[0]]))
    return {
        'top1_agree': bool(baseline and candidates and baseline[0][0] == candidates[0][0]),
        'best_recall': bool(baseline and baseline[0][0] in candidate_ids),
        'top_two_recall': bool(baseline and all(pid in candidate_ids for pid, _ in baseline)),
        'reranked_top1_agree': bool(baseline and reranked and baseline[0][0] == reranked[0][0]),
        'decision_agree': _decision(baseline, threshold, margin) == _decision(reranked, threshold, margin),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('user_id')
    parser.add_argument('--sample', type=int, default=200, help='Number of real faces to query against both methods')
    parser.add_argument('--k', type=int, default=10, help='FAISS candidates to retrieve per query')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--nlist', type=int, default=None,
                        help='Desired IVF list count (reduced to fit the training sample)')
    parser.add_argument('--training-sample', type=int, default=ci.IndexConfig().training_sample_size,
                        help='Maximum deterministic reservoir size (also capped by memory budget)')
    parser.add_argument('--training-iterations', type=int, default=25)
    args = parser.parse_args()

    if (args.sample <= 0 or args.k <= 0 or args.training_sample <= 0 or
            args.training_iterations <= 0 or (args.nlist is not None and args.nlist <= 0)):
        parser.error('Sample sizes, k, nlist and training iterations must be positive')
    config = ci.IndexConfig(training_sample_size=args.training_sample,
                            training_iterations=args.training_iterations, seed=args.seed)
    if args.nlist is not None:
        config = replace(config, nlist_min=args.nlist, nlist_max=args.nlist)
    _get_app()
    print('Shadow only: per-tier/dimension/version stored-representative cosine baseline, '
          'not live assignment parity. Full face-summary metadata and exact FAISS floats '
          'remain resident; budgets apply per build, not aggregate script RSS.')
    builds_by_tier = _build_comparison_indexes(_person_rep_rows_by_tier(args.user_id), config=config)
    if not builds_by_tier:
        print(f'No person representatives found for {args.user_id!r}. Nothing to compare.')
        return

    for key, (build, _exact) in builds_by_tier.items():
        print(f'Built compatible index {key!r}: {build.vector_count} representatives ({build.index_type}).')

    rng = random.Random(args.seed)
    queries = _sample_query_faces(args.user_id, args.sample, rng)
    if not queries:
        print('No clusterable faces found to query. Nothing to compare.')
        return

    embeddings_by_id = app.get_face_embeddings_batch(
        args.user_id, [face_id for face_id, _ in queries],
    )

    metrics = {}
    for face_id, face in queries:
        vector = ci.normalized_vector(face.get('embedding'))
        if vector is None:
            vector = ci.normalized_vector(embeddings_by_id.get(face_id))
        if vector is None:
            continue
        key = (app._face_alignment_tier(face), len(vector), app._face_embedding_version(face))
        pair = builds_by_tier.get(key)
        if pair is None:
            continue
        result = _compare_query(*pair, vector, args.k, app.PEOPLE_CLUSTER_ASSIGN_THRESHOLD,
                                app.PEOPLE_CLUSTER_ASSIGN_MARGIN)
        totals = metrics.setdefault(key, {'compared': 0, **dict.fromkeys(result, 0)})
        totals['compared'] += 1
        for name, value in result.items():
            totals[name] += int(value)

    if not metrics:
        print('No comparable faces (missing embeddings or tier index). Nothing to report.')
        return
    for key, totals in metrics.items():
        print(f'\nCompatible population {key!r}; sampled queries={totals["compared"]}, k={args.k}')
        for name, value in totals.items():
            if name != 'compared':
                print(f'  {name}: {value}/{totals["compared"]} ({100 * value / totals["compared"]:.1f}%)')
        print('  top_two_recall includes both distinct people (or the sole person in a singleton). '
              'decision_agree is exact-reranked threshold/margin sample agreement only; '
              'no ownership, confirmed bonuses or live policy claims.')


if __name__ == '__main__':
    main()
