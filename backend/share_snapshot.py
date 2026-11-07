"""Static snapshot of a public album, served from the storage account's static website.

A shared-album link that lands on a scale-to-zero container shows a blank page for ~30s. The
snapshot is a write-through cache that an always-on origin can serve instead: a small page, a
manifest, and the thumbnails plus previews of the first ``PREVIEW_LIMIT`` photos, all under
``s/<token>/`` in the ``$web`` container. The page wakes the real app in the background and hands
over to it (see ``share_snapshot_page``). Nothing here imports Azure or Flask: callers pass the
container client and the photo source, so the logic is unit-testable.

Eligibility: public, unexpired, no access code. A code-protected album must never be copied to a
public path -- anyone holding the path could view it without the code.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

import share_snapshot_page

logger = logging.getLogger(__name__)

WEB_CONTAINER = '$web'
PREFIX = 's/'
MANIFEST = 'manifest.json'
INDEX = 'index.html'
SCHEMA = 1


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, '') or default))
    except ValueError:
        return default


# How many photos get cached thumbnails + previews. Past this the page hands over to the real app.
PREVIEW_LIMIT = _int_env('SHARE_SNAPSHOT_PREVIEW_LIMIT', 500)
# Previews are ~1.5 MB at most; anything far larger is skipped (the viewer falls back to the thumbnail).
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
# Previews decode full originals; keep this low so a RAW-heavy album cannot exhaust the worker's memory.
WORKERS = max(1, _int_env('SHARE_SNAPSHOT_WORKERS', 4))


def web_base() -> str:
    """Static-website origin (no trailing slash); empty when the feature is not configured."""
    return os.getenv('SHARE_SNAPSHOT_WEB_URL', '').strip().rstrip('/')


def enabled() -> bool:
    return bool(web_base()) and PREVIEW_LIMIT > 0


def prefix(token: str) -> str:
    return f'{PREFIX}{token}/'


def page_url(token: str) -> str:
    return f'{web_base()}/{prefix(token)}'


def public_url(row: Dict, token: str) -> str:
    """The snapshot page URL once it has been built for this token, else ''."""
    if not enabled() or not token:
        return ''
    if str(row.get('shareSnapshotToken') or '') != token or not str(row.get('shareSnapshotAt') or ''):
        return ''
    return page_url(token)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_expired(expires_at: str) -> bool:
    expires = _parse_iso(expires_at) if expires_at else None
    return bool(expires and datetime.now(timezone.utc) > expires)


def eligible(entity: Dict) -> bool:
    if str(entity.get('isPublic', '')).lower() not in ('true', '1'):
        return False
    if not str(entity.get('publicToken') or ''):
        return False
    if str(entity.get('accessCode') or '').strip():
        return False
    return not is_expired(str(entity.get('publicExpiresAt') or ''))


class Photo:
    """One album photo as the snapshot sees it. The readers are lazy so they can run in parallel."""

    def __init__(self, filename: str, *, version: str, is_video: bool = False, rotation: int = 0,
                 thumbnail_rotation: int = 0, read_thumbnail: Callable[[], Optional[bytes]],
                 read_preview: Optional[Callable[[], Optional[bytes]]] = None):
        self.filename = filename
        self.version = version
        self.is_video = is_video
        self.rotation = rotation
        self.thumbnail_rotation = thumbnail_rotation
        self.read_thumbnail = read_thumbnail
        self.read_preview = read_preview


def _content_settings(content_type: str, cache_control: str):
    try:
        from azure.storage.blob import ContentSettings
        return ContentSettings(content_type=content_type, cache_control=cache_control)
    except ImportError:  # unit tests without the Azure SDK
        return {'content_type': content_type, 'cache_control': cache_control}


def _existing_versions(container, base: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for blob in container.list_blobs(name_starts_with=base, include=['metadata']):
        found[blob.name] = str((getattr(blob, 'metadata', None) or {}).get('v', ''))
    return found


def build(entity: Dict, container, photos: Iterable[Photo], total: int, meta: Dict[str, str],
          archive_url: str, spa_url: str) -> Dict[str, int]:
    """Write the snapshot. Returns counts for logging. Assets go first, manifest and page last, so a
    visitor never sees a page that references files that are not there yet."""
    token = str(entity.get('publicToken') or '')
    base = prefix(token)
    existing = _existing_versions(container, base)
    wanted: Dict[str, str] = {}
    photos = list(photos)[:PREVIEW_LIMIT]

    def put(name: str, data: bytes, content_type: str, version: str = '', cache: str = 'public, max-age=3600') -> None:
        container.upload_blob(base + name, data, overwrite=True, metadata={'v': version},
                              content_settings=_content_settings(content_type, cache))

    def sync(index: int, photo: Photo) -> Dict[str, object]:
        entry: Dict[str, object] = {
            'n': photo.filename, 'rot': photo.rotation, 'trot': photo.thumbnail_rotation,
            'video': photo.is_video, 't': '', 'p': '',
        }
        for key, name, reader, limit in (
            ('t', f't/{index}.jpg', photo.read_thumbnail, None),
            ('p', f'p/{index}.jpg', None if photo.is_video else photo.read_preview, PREVIEW_MAX_BYTES),
        ):
            if reader is None:
                continue
            try:
                if existing.get(base + name) != photo.version:
                    data = reader()
                    if not data or (limit and len(data) > limit):
                        continue
                    put(name, data, 'image/jpeg', photo.version)
                entry[key] = name
                wanted[base + name] = photo.version
            except Exception:
                logger.warning('share snapshot: %s for %s failed', name, token, exc_info=True)
        return entry

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        entries = list(pool.map(lambda pair: sync(*pair), enumerate(photos)))

    manifest = {
        'v': SCHEMA, 'name': meta.get('title', ''), 'total': total, 'cached': len(entries),
        'expiresAt': str(entity.get('publicExpiresAt') or ''), 'photos': entries,
    }
    put(MANIFEST, json.dumps(manifest, separators=(',', ':')).encode('utf-8'), 'application/json', cache='no-cache')
    first_preview = bool(entries and entries[0]['p'])
    page = share_snapshot_page.render(
        meta, token=token, spa_url=spa_url, archive_url=archive_url,
        image=(page_url(token) + 'p/0.jpg') if first_preview else meta.get('image', ''),
        image_is_fallback=not first_preview and bool(meta.get('image_is_fallback')),
    )
    put(INDEX, page.encode('utf-8'), 'text/html; charset=utf-8', cache='no-cache')
    wanted[base + MANIFEST] = ''
    wanted[base + INDEX] = ''

    stale = [name for name in existing if name not in wanted]
    for name in stale:
        _delete(container, name)
    return {'cached': len(entries), 'total': total, 'removed': len(stale)}


def _delete(container, name: str) -> None:
    try:
        container.delete_blob(name)
    except Exception as exc:  # already gone is the normal case
        if 'BlobNotFound' not in type(exc).__name__ and 'not found' not in str(exc).lower():
            logger.warning('share snapshot: delete %s failed', name, exc_info=True)


def revoke_page(container, token: str) -> None:
    """Take the entry points down immediately; the rest goes via delete()."""
    for name in (INDEX, MANIFEST):
        _delete(container, prefix(token) + name)


def delete(container, token: str) -> int:
    """Remove every snapshot file for a token. Returns how many blobs were removed."""
    if not token:
        return 0
    names = [blob.name for blob in container.list_blobs(name_starts_with=prefix(token))]
    revoke_page(container, token)   # entry points first, then the assets
    entry_points = {prefix(token) + INDEX, prefix(token) + MANIFEST}
    for name in names:
        if name not in entry_points:
            _delete(container, name)
    return len(names)


def sweep_expired(container) -> List[str]:
    """Delete snapshots whose manifest says the share has expired. Returns the tokens removed."""
    removed: List[str] = []
    for blob in list(container.list_blobs(name_starts_with=PREFIX)):
        if not blob.name.endswith('/' + MANIFEST):
            continue
        try:
            manifest = json.loads(container.download_blob(blob.name).readall())
        except Exception:
            logger.warning('share snapshot: unreadable manifest %s', blob.name, exc_info=True)
            continue
        if is_expired(str(manifest.get('expiresAt') or '')):
            token = blob.name[len(PREFIX):-len(MANIFEST) - 1]
            delete(container, token)
            removed.append(token)
    return removed
