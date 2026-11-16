"""Download library: the manifest a browser needs to copy a whole library straight from Blob Storage.

A direct admin action with its own code (see docs/library-download-dedicated.md). The browser fetches
this once, saves it next to the files, and from then on talks only to Blob Storage; the single call it
makes back during a transfer is for a fresh read token.

The manifest is one streamed pass over the library's metadata partition, one JSON object per line, so
a 50,000-photo library is one request in bounded memory instead of 100 paged calls spread over the run.
Each line carries what the client needs to place, resume and verify a file without asking again: the
blob name, the relative path to save it under, its size and its upload-time SHA-256.

Nothing here imports app: routes hand in the table clients.
"""
import json
import re
from typing import Dict, Iterable, Iterator, Optional

from ordering_utils import metadata_capture_datetime

PAGE = 1000
# exifData is the one heavy column; it is what the capture date (and so the folder) comes from.
FIELDS = ['PartitionKey', 'RowKey', 'anonymousImageId', 'size', 'fileHash', 'processing_state', 'uploadDate',
          'clientLastModified', 'exifData', 'livePhotoOf', 'caption', 'rating', 'likedBy', 'peopleIds']
UNDATED = 'undated'
# Characters no common filesystem accepts in a name, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def quote(value) -> str:
    return str(value).replace("'", "''")


def safe_name(filename: str) -> str:
    """A name every desktop filesystem accepts; the original stays in the manifest as `name`."""
    name = _ILLEGAL.sub('_', str(filename)).strip().rstrip('. ')
    stem = name.split('.', 1)[0].upper()
    if not name or stem in _RESERVED:
        name = '_' + name
    return name[:200]


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _folder(row: Dict) -> str:
    try:
        taken = metadata_capture_datetime(row)
    except Exception:
        taken = None
    return f'{taken.year:04d}/{taken.month:02d}' if taken else UNDATED


def _int(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def entries(metadata_table, library_id: str) -> Iterator[Dict]:
    """One dict per downloadable file, then a trailer with the totals.

    Paths are `YYYY/MM/<name>` by capture date. A Live Photo clip goes beside its still when the still
    was listed first (rows arrive in filename order, where IMG_1.HEIC precedes IMG_1.MOV); otherwise it
    is filed by its own date. Two names that collide after sanitizing get a numeric suffix."""
    rows = metadata_table.query_entities(f"PartitionKey eq '{quote(library_id)}'", select=FIELDS,
                                         results_per_page=PAGE)
    taken, folders = set(), {}
    files = total = unsized = 0
    for row in rows:
        name = str(row.get('RowKey') or '')
        if not name or str(row.get('processing_state') or '').strip().lower() == 'deleted':
            continue
        still = str(row.get('livePhotoOf') or '')
        folder = folders.get(still) or _folder(row)
        folders[name] = folder
        base = safe_name(name)
        path, attempt = f'{folder}/{base}', 1
        while path.lower() in taken:  # case-insensitive filesystems
            attempt += 1
            stem, dot, ext = base.rpartition('.')
            path = f'{folder}/{stem}-{attempt}.{ext}' if dot else f'{folder}/{base}-{attempt}'
        taken.add(path.lower())
        size = _int(row.get('size'))
        entry = {'name': name, 'blob': str(row.get('anonymousImageId') or '').strip() or name, 'path': path}
        if size is not None:
            entry['size'] = size
            total += size
        else:
            unsized += 1
        digest = str(row.get('fileHash') or '').strip().lower()
        if re.fullmatch(r'[0-9a-f]{64}', digest):
            entry['sha256'] = digest
        if still:
            entry['livePhotoOf'] = still
        caption = str(row.get('caption') or '').strip()
        if caption:
            entry['caption'] = caption
        if _int(row.get('rating')):
            entry['rating'] = _int(row.get('rating'))
        if library_id in _json_list(row.get('likedBy')):
            entry['liked'] = True
        people = [str(p) for p in _json_list(row.get('peopleIds')) if p]
        if people:
            entry['people'] = people
        files += 1
        yield entry
    yield {'end': True, 'files': files, 'bytes': total, 'unsized': unsized}


def ndjson(items: Iterable[Dict]) -> Iterator[bytes]:
    """Lines batched into ~64 KB chunks so a 50k-file manifest is hundreds of writes, not 50,000."""
    buffer, size = [], 0
    for item in items:
        line = json.dumps(item, separators=(',', ':'), ensure_ascii=False) + '\n'
        buffer.append(line)
        size += len(line)
        if size >= 65536:
            yield ''.join(buffer).encode('utf-8')
            buffer, size = [], 0
    if buffer:
        yield ''.join(buffer).encode('utf-8')


def library_info(albums_table, people_table, library_id: str, album_names) -> Dict:
    """Albums (with their photos) and people names, for the `library.json` written beside the files.
    `album_names(entity)` is album_store.all_names."""
    pk = quote(library_id)
    albums = []
    if albums_table is not None:
        for row in albums_table.query_entities(f"PartitionKey eq '{pk}'"):
            albums.append({'id': str(row.get('RowKey') or ''), 'name': str(row.get('name') or '').strip(),
                           'photos': list(album_names(row))})
    people = []
    if people_table is not None:
        for row in people_table.query_entities(f"PartitionKey eq '{pk}'", select=['RowKey', 'name']):
            name = str(row.get('name') or '').strip()
            if name:
                people.append({'id': str(row.get('RowKey') or ''), 'name': name})
    return {'albums': albums, 'people': people}
