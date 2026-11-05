"""Uninterrupted reference orchestration for the existing real FAISS matcher.

Intentionally does not use batch(), restart, checkpoint or production queue
grouping. This is the comparison target for legacy chunk and journaled wave execution;
source fencing and publication are covered by their integration tests. Fixed fixtures also assert expected
identity relationships so comparing two equally wrong runs isn't sufficient.
"""
import json

from clustering_wave import canonical_order


def canonical_work(harness, ids):
    faces = {face_id: harness.faces.rows['u', face_id] for face_id in ids}
    return tuple(canonical_order(faces))


def assignment_snapshot(harness):
    people = {}
    for (library, person_id), row in harness.people.rows.items():
        assert library == 'u'
        people[person_id] = {
            'name': row.get('name'),
            'faceIds': sorted(json.loads(row.get('faceIds') or '[]')),
            'representative': json.loads(row.get('repEmbedding') or 'null'),
            'tier': row.get('repEmbeddingTier'),
            'version': row.get('embeddingVersion'),
        }
    return {
        'owners': {face_id: row.get('personId', '')
                   for (library, face_id), row in harness.faces.rows.items()
                   if library == 'u'},
        'people': people,
        'members': sorted((person_id, face_id)
                          for person_id, face_id in harness.members.rows),
        'projectedFiles': sorted(set(harness.metadata)),
    }


def run_single_pass_reference(harness, ids):
    """Return exact ownership/membership/representative output in canonical order."""
    for face_id in canonical_work(harness, ids):
        face = harness.faces.rows['u', face_id]
        harness.assigner.assign('u', face['filename'], [face_id])
    return assignment_snapshot(harness)
