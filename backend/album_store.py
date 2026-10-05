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
