"""One person's photos, best face first, read from the person-membership table.

``photopersonmembers`` has one row per (person, face). Rows now also carry the face's ``filename``,
``confidence`` and ``confirmed`` flag, so a person's whole ranked photo list is ONE partition read -- no
scan of the library's face table. Rows written before this lack those columns; they are filled in the first
time the person is opened (point reads of just that person's faces, written back), so each person pays that
once and everything after is a single query.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

CACHE_TTL_SECONDS = 45
CACHE_MAX = 64
HYDRATE_WORKERS = 16

_CACHE: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
_LOCK = threading.Lock()


def _quote(text: str) -> str:
    return str(text).replace("'", "''")


def _as_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value) -> bool:
    return str(value).strip().lower() in ('1', 'true', 'yes')


def attributes_for_face(face: Dict) -> Dict[str, object]:
    """The columns stored on a membership row for ``face``."""
    confirmed = _truthy(face.get('confirmedByUser')) or str(face.get('reviewStatus') or '').lower() == 'confirmed'
    return {
        'filename': str(face.get('filename') or ''),
        'confidence': _as_float(face.get('confidence')),
        'confirmed': '1' if confirmed else '',
        'sourceDeleted': '1' if _truthy(face.get('sourceDeleted')) else '',
    }


def invalidate(user_id: str, person_id: str) -> None:
    with _LOCK:
        _CACHE.pop((user_id, person_id), None)


def ranked_filenames(user_id: str, person_id: str, members_table, face_table, *, use_cache: bool = True) -> List[str]:
    """Every distinct photo of the person, best face first (confirmed, then confidence, then name)."""
    key = (user_id, person_id)
    now = time.monotonic()
    if use_cache:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    rows = [dict(r) for r in members_table.query_entities(f"PartitionKey eq '{_quote(person_id)}'")]
    missing = [r for r in rows if not r.get('filename')]
    if missing and face_table is not None:
        def hydrate(row: Dict) -> Optional[Dict]:
            try:
                face = face_table.get_entity(partition_key=user_id, row_key=str(row['RowKey']))
            except Exception:
                return None
            if str(face.get('personId') or '') != person_id:
                return None                                   # no longer this person's face
            attrs = attributes_for_face(face)
            try:
                members_table.upsert_entity({'PartitionKey': person_id, 'RowKey': row['RowKey'], **attrs}, mode='merge')
            except Exception:
                pass                                          # still usable this time; retried next open
            return attrs
        with ThreadPoolExecutor(max_workers=min(HYDRATE_WORKERS, len(missing))) as pool:
            for row, attrs in zip(missing, pool.map(hydrate, missing)):
                if attrs:
                    row.update(attrs)
    ranked = sorted(
        (r for r in rows if r.get('filename') and not _truthy(r.get('sourceDeleted'))),
        key=lambda r: (0 if _truthy(r.get('confirmed')) else 1, -_as_float(r.get('confidence')), str(r['filename'])),
    )
    seen = set()
    names: List[str] = []
    for row in ranked:
        name = str(row['filename'])
        if name not in seen:
            seen.add(name)
            names.append(name)
    with _LOCK:
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[key] = (now, names)
    return names
