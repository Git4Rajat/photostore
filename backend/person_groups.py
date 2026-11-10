"""Where a person's overflow rows are recorded.

A person whose membership list filled its table column spills faces into overflow rows tagged
``groupId == <first person's id>``. Finding them with a ``groupId`` filter scans the whole library's person
partition (about 0.1 s for 2,700 people and growing), so the first person's row also carries ``shardIds``: a
JSON list read with the row itself. Code that creates, re-tags or undoes a group keeps it current; clustering
creates rows through its own journaled writer, so the People index build adds any ids that are missing.
The list only ever grows by refresh: a stale id costs one failed point read, a missing one hides faces.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List


def shard_ids(row: Dict) -> List[str]:
    """Overflow row ids recorded on ``row`` (the first person's row), without the person's own id."""
    try:
        listed = json.loads(row.get('shardIds') or '[]')
    except (TypeError, ValueError):
        return []
    own = str(row.get('RowKey') or '')
    return sorted({str(item) for item in listed if item and str(item) != own}) if isinstance(listed, list) else []


def encode(ids: Iterable[str]) -> str:
    return json.dumps(sorted({str(item) for item in ids if item}), separators=(',', ':'))


def missing_pointers(rows: Iterable[Dict]) -> Dict[str, str]:
    """{first person's id: new ``shardIds``} for every group whose recorded list lacks an overflow row that
    its ``groupId`` tags show. Existing entries are kept."""
    rows = list(rows)
    found: Dict[str, set] = {}
    for row in rows:
        group, own = str(row.get('groupId') or ''), str(row.get('RowKey') or '')
        if group and own and group != own:
            found.setdefault(group, set()).add(own)
    by_id = {str(row.get('RowKey') or ''): row for row in rows}
    out: Dict[str, str] = {}
    for group, ids in found.items():
        root = by_id.get(group)
        if root is None:
            continue                                     # the first person is gone; nothing to record on
        have = set(shard_ids(root))
        if not ids <= have:
            out[group] = encode(have | ids)
    return out
