#!/usr/bin/env python3
"""Read-only validation for PEOPLE_REP_BOUNDED_EXEMPLARS before enabling it.

_compute_rep_embedding_for_face_ids currently averages EVERY face a person
owns on every single new-face assignment to them. PEOPLE_REP_BOUNDED_EXEMPLARS
(app.py) caps that to a bounded, confidence/confirmed-prioritized sample
instead -- a real win for a popular person's refresh cost, but it changes
what embedding every future match compares against. Per the project's own
"validate ML changes with real embeddings, not synthetic" rule, this script
computes both the full-membership and bounded-exemplar rep embeddings for
real people in a real library and reports the cosine similarity between them,
so the drift can be judged against real data before flipping the flag on.
It makes no writes -- both embeddings are computed in memory via the exact
same app._compute_rep_embedding/_select_bounded_exemplar_ids helpers the live
code path uses, never stored.

Usage:
    python scripts/compare_bounded_exemplars.py <user_id>                 # every named person with >cap faces
    python scripts/compare_bounded_exemplars.py <user_id> --cap 50
    python scripts/compare_bounded_exemplars.py <user_id> --person-id <id>
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = None


def _get_app():
    global app
    if app is None:
        import app as application
        app = application
    return app


def _full_rep(user_id, face_ids, person_id, summary):
    """Standalone script only: force the reference full, restoring on failure."""
    previous = app.PEOPLE_REP_BOUNDED_EXEMPLARS
    try:
        app.PEOPLE_REP_BOUNDED_EXEMPLARS = False
        return app._compute_rep_embedding_for_face_ids(user_id, face_ids, person_id, summary)
    finally:
        app.PEOPLE_REP_BOUNDED_EXEMPLARS = previous


def _load_face_summary(user_id):
    return app._load_user_face_summary_by_id(user_id)


def _candidate_people(user_id, person_id, cap):
    people = []
    if app.person_table_client is None:
        return people
    pk = app._escape_odata(user_id)
    for row in app.person_table_client.query_entities(f"PartitionKey eq '{pk}'"):
        pid = str(row.get('RowKey') or '')
        if person_id and pid != person_id:
            continue
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        if not person_id and len(face_ids) <= cap:
            continue  # bounding only changes behavior above the cap
        people.append((pid, str(row.get('name') or ''), [str(f) for f in face_ids]))
    return people


def _cosine(a, b):
    try:
        import numpy as np
    except Exception:
        return None
    va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return None
    return float(np.dot(va, vb) / (na * nb))


def main():
    _get_app()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('user_id')
    parser.add_argument('--person-id', default=None)
    parser.add_argument('--cap', type=int, default=app.PEOPLE_REP_BOUNDED_EXEMPLARS_CAP)
    args = parser.parse_args()
    if args.cap <= 0:
        parser.error('--cap must be positive')

    summary = _load_face_summary(args.user_id)
    people = _candidate_people(args.user_id, args.person_id, args.cap)
    if not people:
        print(f'No person over the cap ({args.cap} faces) found for {args.user_id!r} '
              '(or no matching --person-id). Nothing to compare.')
        return

    print(f'Comparing full-membership vs. bounded-exemplar (cap={args.cap}) rep embeddings '
          f'for {len(people)} people in {args.user_id!r}.\n')

    similarities = []
    for person_id, name, face_ids in people:
        full_rep = _full_rep(args.user_id, face_ids, person_id, summary)

        bounded_ids = app._select_bounded_exemplar_ids(face_ids, summary, args.cap)
        bounded_rep = app._compute_rep_embedding_for_face_ids(args.user_id, bounded_ids, person_id, summary)

        sim = _cosine(full_rep, bounded_rep)
        similarities.append(sim)
        sim_str = f'{sim:.4f}' if sim is not None else 'n/a'
        print(f'  {name or "(unnamed)":30s} id={person_id}  faces={len(face_ids):5d}  '
              f'exemplars={len(bounded_ids):4d}  cosine(full, bounded)={sim_str}')

    valid = [s for s in similarities if s is not None]
    if valid:
        print(f'\nMean cosine similarity: {sum(valid) / len(valid):.4f}  '
              f'min: {min(valid):.4f}  over {len(valid)} people.')
        print('Lower similarity means more drift from enabling bounded exemplars -- judge against your '
              'matching thresholds (PEOPLE_MATCH_THRESHOLD / PEOPLE_CLUSTER_ASSIGN_THRESHOLD) before enabling.')


if __name__ == '__main__':
    main()
