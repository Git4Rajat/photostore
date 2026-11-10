"""Repair a person row whose ``faceIds`` list lags behind the faces that point at it.

A merge writes the face rows and the membership rows first and the survivor's ``faceIds`` last. If the request
is cut off in between (the ingress closes it after 240 s), the faces already belong to the person -- the photo
list reads the membership table and shows them -- but the People tile counts ``faceIds`` and does not.

``photopersonmembers`` is only a candidate list (it is dual-written best effort); a face is added back only
when its own row still names this person and is not rejected.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Iterable, List, Optional

LEGACY_MEMBERSHIP_BYTES = 60 * 1024   # same ceiling as faiss_assignment: one UTF-16 table column


def _quote(text: str) -> str:
    return str(text).replace("'", "''")


def encode_face_ids(face_ids: List[str]) -> str:
    return json.dumps(face_ids, ensure_ascii=True, separators=(',', ':'))


def fits(face_ids: List[str]) -> bool:
    return len(encode_face_ids(face_ids).encode('utf-16-le')) <= LEGACY_MEMBERSHIP_BYTES


def missing_face_ids(person_id: str, person_row: Dict, members_table,
                     load_faces: Callable[[List[str]], Dict[str, Dict]],
                     is_rejected: Callable[[Dict], bool]) -> List[str]:
    """Faces the membership table lists for the person and whose own row still names it, but that
    ``faceIds`` lacks. Order follows the membership rows."""
    try:
        listed = [str(face_id) for face_id in json.loads(person_row.get('faceIds') or '[]')]
    except (TypeError, ValueError):
        return []
    have = set(listed)
    candidates = [str(row['RowKey']) for row in
                  members_table.query_entities(f"PartitionKey eq '{_quote(person_id)}'", select=['RowKey'])
                  if str(row['RowKey']) not in have]
    if not candidates:
        return []
    faces = load_faces(candidates)
    return [face_id for face_id in candidates
            if face_id in faces and str(faces[face_id].get('personId') or '') == person_id
            and not is_rejected(faces[face_id])]


def reconcile_people(user_id: str, person_rows: Iterable[Dict], members_table,
                     load_faces: Callable[[List[str]], Dict[str, Dict]], is_rejected: Callable[[Dict], bool],
                     add_face_ids: Callable[[str, List[str]], None], *, dry_run: bool = True,
                     should_stop: Optional[Callable[[], bool]] = None) -> Dict:
    """Check each person row; unless ``dry_run``, write back the repaired ``faceIds``. A person whose repaired
    list would not fit one column is reported under ``overflow`` and left alone. ``add_face_ids`` must union
    into the live row (a face added meanwhile must survive). ``should_stop`` lets a caller
    bound the run; ``lastChecked`` is then the RowKey to resume after."""
    repaired, overflow, checked, last = [], [], 0, ''
    for row in person_rows:
        if should_stop and should_stop():
            return dict(complete=False, checked=checked, lastChecked=last, repaired=repaired, overflow=overflow)
        person_id = str(row['RowKey'])
        checked += 1
        last = person_id
        extra = missing_face_ids(person_id, row, members_table, load_faces, is_rejected)
        if not extra:
            continue
        merged = [str(face_id) for face_id in json.loads(row.get('faceIds') or '[]')] + extra
        if not fits(merged):
            overflow.append(dict(personId=person_id, missing=len(extra)))
            continue
        if not dry_run:
            add_face_ids(person_id, extra)
        repaired.append(dict(personId=person_id, name=str(row.get('name') or ''), added=len(extra),
                             before=len(merged) - len(extra), after=len(merged)))
    return dict(complete=True, checked=checked, lastChecked=last, repaired=repaired, overflow=overflow)
