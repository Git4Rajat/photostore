#!/usr/bin/env python3
"""Read-only diagnostic for a person/cluster's faces.

For each matched person it prints the stored ``faceIds`` and, per face, the fields
that decide whether reclustering will keep the face glued to the person:

  * ``personId``            -- current owner (differs from the person => already unmerged)
  * ``confirmedByUser``     -- sticky
  * ``assignedByPropagation`` -- sticky
  * ``reviewStatus`` / ``rejected``
  * ``embeddingVersion``    -- excluded from clustering when not in the allowed set

It also summarises how many faces are *active* (still owned by the person),
*sticky*, and *version-allowed* — i.e. how many would survive a recluster BEFORE
the named-cluster protection fix, and flags faces that already drifted to another
owner. Makes no writes.

Usage:
    python scripts/diagnose_person.py <user_id>                  # list named clusters
    python scripts/diagnose_person.py <user_id> --name Rajat     # match by name (substring, case-insensitive)
    python scripts/diagnose_person.py <user_id> --person-id <id> # match by exact id
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402  (imports run _init_storage_clients() at module load)


def _load_faces_by_id(user_id):
    faces = {}
    if app.face_table_client is None:
        return faces
    pk = app._escape_odata(user_id)
    for row in app.face_table_client.query_entities(f"PartitionKey eq '{pk}'"):
        faces[str(row.get('RowKey') or '')] = dict(row)
    return faces


def _match_people(user_id, name, person_id):
    people = []
    if app.person_table_client is None:
        return people
    pk = app._escape_odata(user_id)
    for row in app.person_table_client.query_entities(f"PartitionKey eq '{pk}'"):
        pid = str(row.get('RowKey') or '')
        pname = str(row.get('name') or '')
        if person_id and pid != person_id:
            continue
        if name and name.lower() not in pname.lower():
            continue
        if not person_id and not name and app._is_unnamed_name(pname):
            continue  # default listing shows only named clusters
        people.append(dict(row))
    return people


def _describe_person(person, faces_by_id):
    user_id = str(person.get('PartitionKey') or '')
    pid = str(person.get('RowKey') or '')
    pname = str(person.get('name') or '')
    allowed = app._face_embedding_allowed_versions()
    try:
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []

    print('=' * 88)
    print(f"Person: {pname!r}  id={pid}  named={app._person_entity_is_named(person)}")
    print(f"Stored faceIds: {len(face_ids)}    allowed embedding versions: {sorted(allowed) or '(any)'}")
    print('-' * 88)

    active = sticky = version_ok = drifted = missing = 0
    for fid in face_ids:
        fid = str(fid)
        face = faces_by_id.get(fid)
        if face is None:
            print(f"  {fid}  <MISSING FACE ROW>")
            missing += 1
            continue
        owner = str(face.get('personId') or '')
        is_owned = app._face_is_owned_by_person(face, pid)
        rejected = app._face_is_rejected(face)
        confirmed = app._face_is_confirmed(face)
        prop = app._face_is_propagation_assigned(face)
        ver = app._face_embedding_version(face)
        ver_ok = app._face_embedding_allowed_for_clustering(face)
        is_sticky = app._face_assignment_is_sticky(face)

        if is_owned and not rejected:
            active += 1
        if is_sticky:
            sticky += 1
        if ver_ok:
            version_ok += 1
        if owner and owner != pid:
            drifted += 1

        flags = []
        if not is_owned:
            flags.append(f"OWNER={owner or 'none'}")
        if rejected:
            flags.append('REJECTED')
        if confirmed:
            flags.append('confirmed')
        if prop:
            flags.append('propagation')
        if not ver_ok:
            flags.append(f"VER!={ver or 'none'}")
        if not is_sticky and not rejected:
            flags.append('NON-STICKY')
        print(f"  {fid}  ver={ver or '-':<8} {' '.join(flags) if flags else 'owned/sticky/ok'}")

    print('-' * 88)
    print(f"active(owned)={active}  sticky={sticky}  version-allowed={version_ok}  "
          f"drifted-to-other-owner={drifted}  missing={missing}")
    # Faces that a *plain* recluster would have re-pooled (and could scatter) before
    # the named-cluster protection fix: owned, non-sticky, version-allowed.
    at_risk = sum(
        1 for fid in face_ids
        if (f := faces_by_id.get(str(fid))) is not None
        and app._face_is_owned_by_person(f, pid)
        and not app._face_is_rejected(f)
        and not app._face_assignment_is_sticky(f)
        and app._face_embedding_allowed_for_clustering(f)
    )
    print(f"non-sticky faces a recluster could have scattered (pre-fix): {at_risk}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('user_id', help='library / user partition key')
    parser.add_argument('--name', default='', help='match person name (substring, case-insensitive)')
    parser.add_argument('--person-id', default='', help='match exact person id')
    args = parser.parse_args()

    if app.person_table_client is None or app.face_table_client is None:
        print('ERROR: people tables unavailable — is the Azure storage env configured?', file=sys.stderr)
        return 2

    people = _match_people(args.user_id, args.name.strip(), args.person_id.strip())
    if not people:
        print('No matching person found.')
        return 1

    faces_by_id = _load_faces_by_id(args.user_id)
    for person in people:
        _describe_person(person, faces_by_id)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
