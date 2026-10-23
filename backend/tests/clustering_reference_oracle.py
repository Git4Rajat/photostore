"""Uninterrupted reference orchestration for the existing real FAISS matcher.

Intentionally does not use batch(), restart, checkpoint or production queue
grouping. This qualifies current chunk orchestration, not the future wave
decision journal/source-fencing protocol. Fixed fixtures also assert expected
identity relationships so comparing two equally wrong runs isn't sufficient.
"""
import json


def canonical_work(harness, ids):
    def key(face_id):
        face = harness.faces.rows['u', face_id]
        vector = json.loads(face['embedding'])
        return (face['alignmentMethod'], face['embeddingVersion'], len(vector),
                face['filename'], face_id)
    return tuple(sorted(ids, key=key))


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
