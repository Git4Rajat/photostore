"""How an album's photo list is stored on its Table Storage row.

Azure Table Storage caps a single string property at 64 KB (32K UTF-16 characters) and a whole row at
1 MB. The list used to be ONE JSON string in ``filenames``, so an album silently topped out at about
1,500 photos and a bigger write (a smart album, a large selection) failed with a 400. The JSON text is
now split across ``filenames``, ``filenames_1`` ... ``filenames_N`` (each under the property cap), which
lifts the limit to ``MAX_CHARS`` of JSON -- roughly 14,000 photos -- while staying one row, one point
read and one transaction, so nothing else about albums changes. Rows written before this have only
``filenames`` and read back unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

CHUNK_UTF16_UNITS = 28000      # < 32K, leaves headroom for escaping
MAX_CHUNKS = 14                # ~392K UTF-16 units ~= 784 KB, under the 1 MB row limit
MAX_UNITS = CHUNK_UTF16_UNITS * MAX_CHUNKS


class AlbumTooLarge(ValueError):
    """The photo list does not fit in one album row."""


def _key(index: int) -> str:
    return 'filenames' if index == 0 else f'filenames_{index}'


def _units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def read_filenames(entity: Dict) -> List[str]:
    parts = []
    for index in range(MAX_CHUNKS):
        value = entity.get(_key(index))
        if value is None or value == '':
            if index == 0:
                continue
            break
        parts.append(str(value))
    text = ''.join(parts) or '[]'
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _chunks(text: str) -> List[str]:
    out, current, size = [], [], 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if size + width > CHUNK_UTF16_UNITS:
            out.append(''.join(current))
            current, size = [], 0
        current.append(ch)
        size += width
    if current or not out:
        out.append(''.join(current))
    return out


def write_filenames(entity: Dict, filenames: List[str]) -> None:
    """Store ``filenames`` on ``entity`` (in place). Raises AlbumTooLarge if it cannot fit."""
    text = json.dumps(list(filenames), ensure_ascii=False, separators=(',', ':'))
    chunks = _chunks(text)
    if len(chunks) > MAX_CHUNKS:
        raise AlbumTooLarge(f'An album can hold about {max_photos(filenames)} photos')
    for index, chunk in enumerate(chunks):
        entity[_key(index)] = chunk
    for index in range(len(chunks), MAX_CHUNKS):
        # Table upserts MERGE, so a shrunk list must overwrite the chunks it no longer uses.
        if _key(index) in entity:
            entity[_key(index)] = ''


def max_photos(sample: List[str]) -> int:
    """Approximate capacity for names like the ones in ``sample``."""
    if not sample:
        return MAX_UNITS // 40
    average = max(8, sum(_units(str(n)) + 4 for n in sample[:500]) // min(500, len(sample)))
    return MAX_UNITS // average


def fit(filenames: List[str]) -> Tuple[List[str], bool]:
    """The longest prefix of ``filenames`` that fits in an album, and whether it was cut."""
    names = list(filenames)
    try:
        write_filenames({}, names)
        return names, False
    except AlbumTooLarge:
        pass
    low, high = 0, len(names)
    while low < high:                       # largest prefix that still fits
        mid = (low + high + 1) // 2
        try:
            write_filenames({}, names[:mid])
            low = mid
        except AlbumTooLarge:
            high = mid - 1
    return names[:low], True


# --- membership table ----------------------------------------------------------------------------
#
# An album whose list no longer fits in its row (about 14,000 photos) is "promoted": its photos move to
# one row each in the members table (PartitionKey = album id, RowKey = filename) and the album row keeps
# only ``storage='table'`` and ``photoCount``. Small albums never change. A members-table album lists in
# filename order and pages by walking RowKeys, so nothing about it is bounded by a row size.

_MEMBERS = None
# An album bigger than this lives in the members table even though it would still fit in its row: a row
# holding 10,000+ names is ~0.7 MB that every add/remove rewrites and every index rebuild, photo delete and
# page read has to fetch and parse. Members rows are touched only for the photos that change.
INLINE_MAX_PHOTOS = int(os.getenv('ALBUM_INLINE_MAX_PHOTOS', '2000'))
_MEMBER_LOCK = threading.Lock()
_BATCH = 100
_EXISTS_CHUNK = 15            # Table Storage allows 15 comparisons in one filter
_POOL = 8


def configure(members_table) -> None:
    global _MEMBERS
    _MEMBERS = members_table


def members_available() -> bool:
    return _MEMBERS is not None


def is_table_backed(entity: Dict) -> bool:
    return str(entity.get('storage') or '') == 'table'


def _pk(entity: Dict) -> str:
    return str(entity['RowKey'])


def _quote(text: str) -> str:
    return str(text).replace("'", "''")


def count(entity: Dict) -> int:
    if is_table_backed(entity):
        try:
            return max(0, int(entity.get('photoCount') or 0))
        except (TypeError, ValueError):
            return 0
    return len(read_filenames(entity))


def all_names(entity: Dict) -> List[str]:
    """Every filename, in album order. Streams the members partition for table-backed albums."""
    if not is_table_backed(entity):
        return read_filenames(entity)
    if _MEMBERS is None:
        return []
    return [str(r['RowKey']) for r in _MEMBERS.query_entities(f"PartitionKey eq '{_quote(_pk(entity))}'", select=['RowKey'])]


def page(entity: Dict, offset: int, limit: int) -> Tuple[List[str], int]:
    """``limit`` filenames starting at ``offset`` (0 = no limit) and the album's total."""
    total = count(entity)
    if not is_table_backed(entity):
        names = read_filenames(entity)
        return (names[offset:offset + limit] if limit > 0 else names), total
    if _MEMBERS is None:
        return [], total
    out: List[str] = []
    stop = offset + limit if limit > 0 else None
    for index, row in enumerate(_MEMBERS.query_entities(f"PartitionKey eq '{_quote(_pk(entity))}'", select=['RowKey'])):
        if stop is not None and index >= stop:
            break
        if index >= offset:
            out.append(str(row['RowKey']))
    return out, total


def sample(entity: Dict, limit: int = 2000) -> List[str]:
    """The first ``limit`` filenames, for picking a cover without reading a huge album."""
    return page(entity, 0, limit)[0]


def _existing(entity: Dict, names: List[str]) -> set:
    found: set = set()
    pk = _quote(_pk(entity))
    chunks = [names[i:i + _EXISTS_CHUNK] for i in range(0, len(names), _EXISTS_CHUNK)]

    def look(chunk):
        clause = ' or '.join(f"RowKey eq '{_quote(n)}'" for n in chunk)
        return [str(r['RowKey']) for r in _MEMBERS.query_entities(f"PartitionKey eq '{pk}' and ({clause})", select=['RowKey'])]

    if len(chunks) <= 1:
        results = [look(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=_POOL) as pool:
            results = list(pool.map(look, chunks))
    for result in results:
        found.update(result)
    return found


def contains(entity: Dict, filename: str) -> bool:
    if not is_table_backed(entity):
        return filename in read_filenames(entity)
    if _MEMBERS is None:
        return False
    try:
        _MEMBERS.get_entity(partition_key=_pk(entity), row_key=filename)
        return True
    except Exception:
        return False


def _transact(operations: List[Tuple]) -> None:
    for start in range(0, len(operations), _BATCH):
        chunk = operations[start:start + _BATCH]
        try:
            _MEMBERS.submit_transaction(chunk)
        except Exception:
            for action, row in chunk:                 # transactions are atomic; singles are idempotent
                if action == 'upsert':
                    _MEMBERS.upsert_entity(row)
                else:
                    try:
                        _MEMBERS.delete_entity(partition_key=row['PartitionKey'], row_key=row['RowKey'])
                    except Exception:
                        pass


def _write_members(entity: Dict, names: List[str]) -> None:
    pk = _pk(entity)
    _transact([('upsert', {'PartitionKey': pk, 'RowKey': n}) for n in names])


def _promote(entity: Dict, names: List[str]) -> None:
    if _MEMBERS is None:
        raise AlbumTooLarge(f'An album can hold about {max_photos(names)} photos')
    _write_members(entity, names)
    entity['storage'] = 'table'
    entity['photoCount'] = len(set(names))
    for index in range(MAX_CHUNKS):
        if _key(index) in entity:
            entity[_key(index)] = ''


def add(entity: Dict, names: List[str]) -> List[str]:
    """Add ``names`` (de-duplicated, in order); returns the ones that were new. Mutates ``entity``."""
    names = list(dict.fromkeys(names))
    if is_table_backed(entity):
        if not names or _MEMBERS is None:
            return []
        have = _existing(entity, names)
        new = [n for n in names if n not in have]
        _write_members(entity, new)
        entity['photoCount'] = count(entity) + len(new)
        return new
    current = read_filenames(entity)
    have = set(current)
    new = [n for n in names if n not in have]
    combined = current + new
    if _MEMBERS is not None and len(combined) > INLINE_MAX_PHOTOS:
        _promote(entity, combined)
        return new
    try:
        write_filenames(entity, combined)
    except AlbumTooLarge:
        _promote(entity, combined)
    return new


def remove(entity: Dict, names: List[str]) -> List[str]:
    """Remove ``names``; returns the ones that were present. Mutates ``entity``."""
    names = list(dict.fromkeys(names))
    if is_table_backed(entity):
        if not names or _MEMBERS is None:
            return []
        present = _existing(entity, names)
        gone = [n for n in names if n in present]
        pk = _pk(entity)
        _transact([('delete', {'PartitionKey': pk, 'RowKey': n}) for n in gone])
        entity['photoCount'] = max(0, count(entity) - len(gone))
        return gone
    current = read_filenames(entity)
    drop = set(names)
    gone = [n for n in current if n in drop]
    if gone:
        write_filenames(entity, [n for n in current if n not in drop])
    return gone


def set_all(entity: Dict, names: List[str]) -> None:
    """Replace the whole list on a NEW entity (creation paths)."""
    if _MEMBERS is not None and len(names) > INLINE_MAX_PHOTOS:
        _promote(entity, names)
        return
    try:
        write_filenames(entity, names)
    except AlbumTooLarge:
        _promote(entity, names)


def delete_members(entity: Dict) -> None:
    if not is_table_backed(entity) or _MEMBERS is None:
        return
    pk = _pk(entity)
    rows = [str(r['RowKey']) for r in _MEMBERS.query_entities(f"PartitionKey eq '{_quote(pk)}'", select=['RowKey'])]
    _transact([('delete', {'PartitionKey': pk, 'RowKey': n}) for n in rows])
