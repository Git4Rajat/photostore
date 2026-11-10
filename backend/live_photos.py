"""Live Photos: a still plus a 1.5-3 s motion clip.

Apple exports a Live Photo as two files (``IMG_1234.HEIC`` + ``IMG_1234.MOV``), and both are uploaded. The
clip is attached to its photo instead of becoming a separate video: the clip row gets ``livePhotoOf`` (which
keeps it out of the gallery and search, see catalog_metadata) and the photo row ``livePhotoClip``.

Detection reads Apple's own tags from the clip's browser metadata packet (``com.apple.quicktime.livephoto.*``;
ordinary videos have none). Pairing prefers Apple's content identifier, which both halves carry (the photo in
its maker notes, decoded by the browser's exiftool), then the capture time within ``CAPTURE_TOLERANCE``
seconds among files sharing a base name, then -- while a photo's EXIF is not processed yet -- the exact name
and the files' modified times. Export renames copies (``IMG_1234 (2).MOV``) and uploads de-duplicate names
(``IMG_1234-1a2b3c4d.HEIC``), so the base name drops both suffixes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

CLIP_INFO = 'livePhotoClipInfo'   # on a video: {"contentId", "capturedAt"} when it is a Live Photo clip, else 'none'
CLIP_OF = 'livePhotoOf'           # on an attached clip: its photo's filename
PHOTO_CLIP = 'livePhotoClip'      # on a photo: its attached clip's filename
NOT_A_CLIP = 'none'

PHOTO_EXTENSIONS = frozenset({'heic', 'heif', 'hif', 'jpg', 'jpeg', 'png'})
VIDEO_EXTENSIONS = frozenset({'mov', 'mp4', 'm4v'})
CAPTURE_TOLERANCE = 2.0

# Columns pairing reads (callers project these).
PAIRING_FIELDS = ('PartitionKey', 'RowKey', 'processing_state', 'exifData', 'clientLastModified',
                  CLIP_INFO, CLIP_OF, PHOTO_CLIP)

_COLLISION_SUFFIX = re.compile(r'-[0-9a-f]{8}$')
_COPY_SUFFIX = re.compile(r' \(\d+\)$')


def extension(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def exact_stem(filename: str) -> str:
    """The name without extension or upload collision suffix (export copy numbers kept)."""
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
    return _COLLISION_SUFFIX.sub('', stem)


def base_stem(filename: str) -> str:
    """The name both halves of a Live Photo share, whatever export and upload appended."""
    return _COPY_SUFFIX.sub('', exact_stem(filename))


def clip_info_from_tags(tags: Dict[str, object]) -> Optional[Dict[str, str]]:
    """Live Photo signature from decoded browser video metadata (video_metadata.decode_video_metadata), or None
    for an ordinary video. Tags flatten mediainfo's ``extra`` block as ``MediaInfo.General.0.extra.<key>``."""
    extra = {str(key).rsplit('.extra.', 1)[1]: value for key, value in tags.items() if '.extra.' in str(key)}
    if not any('livephoto' in key for key in extra):
        return None
    return {'contentId': str(extra.get('com_apple_quicktime_content_identifier') or '').strip(),
            'capturedAt': str(tags.get('CreateDate') or '').strip()}


def clip_info(row: Dict) -> Optional[Dict[str, str]]:
    raw = row.get(CLIP_INFO)
    if not raw or raw == NOT_A_CLIP:
        return None
    try:
        info = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def _exif(row: Dict) -> Dict[str, object]:
    raw = row.get('exifData')
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _when(value: object, offset: object = None) -> Optional[Tuple[datetime, Optional[datetime]]]:
    """(local wall-clock time, absolute time or None) of an EXIF or mediainfo date."""
    raw = str(value or '').strip()
    if not raw:
        return None
    absolute = None
    utc_only = raw.endswith(' UTC') or raw.endswith('Z')
    if utc_only:
        raw, absolute_zone = raw[:-4] if raw.endswith(' UTC') else raw[:-1], timezone.utc
    else:
        absolute_zone = None
        match = re.search(r'([+-])(\d\d):?(\d\d)$', raw)
        if match and len(raw) > 19:
            sign = 1 if match.group(1) == '+' else -1
            absolute_zone = timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
            raw = raw[:match.start()]
    raw = raw.replace('-', ':', 2).replace('T', ' ')[:19]
    try:
        wall = datetime.strptime(raw, '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None
    if absolute_zone is None and offset:
        match = re.fullmatch(r'([+-])(\d\d):(\d\d)', str(offset).strip())
        if match:
            sign = 1 if match.group(1) == '+' else -1
            absolute_zone = timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    if absolute_zone is not None:
        absolute = wall.replace(tzinfo=absolute_zone)
    # A UTC-only stamp says nothing about the local wall clock.
    return (None if utc_only else wall), absolute


def _seconds_apart(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    if a[1] is not None and b[1] is not None:
        return abs((a[1] - b[1]).total_seconds())
    if a[0] is not None and b[0] is not None:
        return abs((a[0] - b[0]).total_seconds())
    return None


def _photo_capture(row: Dict):
    exif = _exif(row)
    return _when(exif.get('DateTimeOriginal'), exif.get('OffsetTimeOriginal') or exif.get('OffsetTime'))


def _modified(row: Dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(row.get('clientLastModified') or ''))
    except ValueError:
        return None


def _active(row: Dict) -> bool:
    return str(row.get('processing_state') or '').strip().lower() != 'deleted'


def _score(clip: Dict, info: Dict[str, str], photo: Dict):
    """Lower is better; None when the photo cannot be this clip's still."""
    photo_id = str(_exif(photo).get('ContentIdentifier') or '').strip()
    if info.get('contentId') and photo_id:
        return (0, 0.0) if photo_id == info['contentId'] else None
    same_name = exact_stem(clip['RowKey']) == exact_stem(photo['RowKey'])
    apart = _seconds_apart(_when(info.get('capturedAt')), _photo_capture(photo))
    if apart is not None:
        return (1 if same_name else 2, apart) if apart <= CAPTURE_TOLERANCE else None
    clip_modified, photo_modified = _modified(clip), _modified(photo)
    if same_name and clip_modified and photo_modified:
        gap = abs((clip_modified - photo_modified).total_seconds())
        if gap <= CAPTURE_TOLERANCE:
            return (3, gap)
    return None


def choose_pairs(rows: Iterable[Dict]) -> List[Tuple[str, str]]:
    """(clip, photo) filenames to attach among ``rows`` that share one base name. Attached halves are kept
    as they are; each photo takes at most one clip, best match first."""
    rows = [row for row in rows if _active(row)]
    clips = [(row, clip_info(row)) for row in rows
             if extension(row['RowKey']) in VIDEO_EXTENSIONS and not row.get(CLIP_OF)]
    clips = [(row, info) for row, info in clips if info is not None]
    photos = [row for row in rows if extension(row['RowKey']) in PHOTO_EXTENSIONS and not row.get(PHOTO_CLIP)]
    options = []
    for clip, info in clips:
        for photo in photos:
            score = _score(clip, info, photo)
            if score is not None:
                options.append((score, clip['RowKey'], photo['RowKey']))
    pairs, used_clips, used_photos = [], set(), set()
    for _score_value, clip_name, photo_name in sorted(options):
        if clip_name in used_clips or photo_name in used_photos:
            continue
        used_clips.add(clip_name)
        used_photos.add(photo_name)
        pairs.append((clip_name, photo_name))
    return pairs
