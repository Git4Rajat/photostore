#!/usr/bin/env python3
"""Replace the baked-in "VIDEO" pill on existing video thumbnails with the centred play icon.

Browser uploads used to draw a "VIDEO" pill into the 120px thumbnail itself (stampVideoThumbnail in
frontend/src/utils/uploadDerivatives.ts); new uploads now draw a small centred play icon instead. The pixels
under an old pill are gone, so each thumbnail is rebuilt from its original video: ffmpeg reads one frame
straight from the blob over HTTP range requests (only the bytes it needs, never the whole video), the frame is
scaled to a 120px edge, the play icon is drawn, and the JPEG overwrites thumbnails/<physical blob name>.

Video rows carry no thumbnailArtifact receipt (video preparation is browser-owned), so an in-place overwrite
needs no metadata change. Rows that do carry a receipt are skipped. Each rewritten blob is tagged with the blob
metadata ``playicon=1`` and is skipped on later runs; ``--modified-before`` additionally skips thumbnails the
browser wrote after the new code shipped (they never had a pill). The write is conditional on the blob's ETag, so a
thumbnail replaced meanwhile is left alone.

Dry run by default: lists what would change and, with --preview-dir, writes the new JPEGs locally to eyeball.

Auth: DefaultAzureCredential (your `az login`). Needs Storage Table Data Reader on photometadata and Storage
Blob Data Contributor on the images and thumbnails containers. Needs ffmpeg (--ffmpeg, FFMPEG, PATH, or the
imageio_ffmpeg wheel).

Usage:
  python scripts/restamp_video_thumbnails.py --account keepsakedwfgani6azekc --library <user id> \\
      --preview-dir /tmp/restamp --limit 20                      # dry run, writes sample JPEGs only
  python scripts/restamp_video_thumbnails.py --account ... --library ... \\
      --modified-before 2026-11-10T00:00:00Z --limit 50 --apply  # small batch first
Browsers keep thumbnails in an IndexedDB cache for up to 7 days, so a device can show the old pill until then.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from azure.core import MatchConditions
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from image_utils import is_video_file

THUMBNAIL_EDGE = 120
THUMBNAIL_QUALITY = 65
SUPERSAMPLE = 4
MARKER = 'playicon'
STORAGE_SCOPE = 'https://storage.azure.com/.default'
STORAGE_API_VERSION = '2023-11-03'
FFMPEG_TIMEOUT_SECONDS = 60


def find_ffmpeg(explicit=''):
    found = explicit or os.getenv('FFMPEG') or shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit('ffmpeg not found: pass --ffmpeg, set FFMPEG, or install it')


def thumbnail_size(width, height, edge=THUMBNAIL_EDGE):
    """Same sizing rule as uploadDerivativeSize in the frontend."""
    scale = min(1, edge / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def stamp_play_icon(frame, edge=THUMBNAIL_EDGE):
    """Scale ``frame`` to the thumbnail size and draw the centred play icon, as stampVideoThumbnail does:
    a translucent dark disc (radius 18% of the short side) with a white triangle nudged right of centre."""
    width, height = thumbnail_size(*frame.size, edge)
    image = frame.convert('RGB').resize((width, height), Image.LANCZOS).convert('RGBA')
    scale = SUPERSAMPLE
    overlay = Image.new('RGBA', (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    radius = max(6, round(min(width, height) * 0.18)) * scale
    cx, cy = width * scale / 2, height * scale / 2
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(0, 0, 0, round(255 * 0.45)))
    half = radius * 0.45
    left = cx - half * 0.6
    draw.polygon([(left, cy - half), (left + half * 1.6, cy), (left, cy + half)], fill=(255, 255, 255, round(255 * 0.95)))
    overlay = overlay.resize((width, height), Image.LANCZOS)
    return Image.alpha_composite(image, overlay).convert('RGB')


def encode_thumbnail(frame):
    out = io.BytesIO()
    stamp_play_icon(frame).save(out, format='JPEG', quality=THUMBNAIL_QUALITY)
    return out.getvalue()


def extract_frame(ffmpeg, url, token):
    """One frame about 1s in (the browser path uses min(1s, 10% of the duration)), else the first frame.
    ffmpeg autorotates, matching the browser's <video> frame. Returns a PIL image or None."""
    headers = f'Authorization: Bearer {token}\r\nx-ms-version: {STORAGE_API_VERSION}\r\n'
    for seek in ('1', '0'):
        try:
            result = subprocess.run(
                [ffmpeg, '-v', 'error', '-headers', headers, '-ss', seek, '-i', url, '-frames:v', '1',
                 '-f', 'image2pipe', '-vcodec', 'png', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT_SECONDS, check=False)
        except Exception:
            return None
        if result.returncode == 0 and result.stdout:
            try:
                image = Image.open(io.BytesIO(result.stdout))
                image.load()
                return image
            except Exception:
                continue
    return None


def video_rows(table, library):
    columns = ['RowKey', 'anonymousImageId', 'thumbnail_status', 'thumbnailArtifact', 'blobName']
    for row in table.query_entities(f"PartitionKey eq '{library}'", select=columns):
        if is_video_file(str(row['RowKey'])):
            yield row


def run(args):
    credential = DefaultAzureCredential()
    ffmpeg = find_ffmpeg(args.ffmpeg)
    tables = TableServiceClient(f'https://{args.account}.table.core.windows.net', credential=credential)
    blobs = BlobServiceClient(f'https://{args.account}.blob.core.windows.net', credential=credential)
    table = tables.get_table_client('photometadata')
    cutoff = datetime.fromisoformat(args.modified_before.replace('Z', '+00:00')) if args.modified_before else None
    if args.apply and cutoff is None:
        raise SystemExit('--apply requires --modified-before (the time the play-icon build was deployed)')
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    if args.preview_dir:
        os.makedirs(args.preview_dir, exist_ok=True)

    counts = dict(seen=0, skipped_status=0, skipped_receipt=0, skipped_done=0, skipped_new=0, skipped_missing=0,
                  no_frame=0, would_change=0, changed=0, conflict=0, failed=0)
    pending = []
    for row in video_rows(table, args.library):
        counts['seen'] += 1
        if str(row.get('thumbnail_status') or '') != 'done':
            counts['skipped_status'] += 1
        elif row.get('thumbnailArtifact'):
            counts['skipped_receipt'] += 1
        else:
            pending.append(row)
    if args.after:
        pending = [row for row in pending if str(row['RowKey']) > args.after]
    pending.sort(key=lambda row: str(row['RowKey']))

    def process(row):
        name = str(row.get('anonymousImageId') or row['RowKey'])
        thumb = blobs.get_blob_client('thumbnails', name)
        try:
            props = thumb.get_blob_properties()
        except Exception:
            return 'skipped_missing', row
        if (props.metadata or {}).get(MARKER):
            return 'skipped_done', row
        if cutoff is not None and props.last_modified >= cutoff:
            return 'skipped_new', row
        token = credential.get_token(STORAGE_SCOPE).token
        frame = extract_frame(ffmpeg, f'https://{args.account}.blob.core.windows.net/images/{name}', token)
        if frame is None:
            return 'no_frame', row
        data = encode_thumbnail(frame)
        if args.preview_dir:
            with open(os.path.join(args.preview_dir, f"{str(row['RowKey'])}.jpg"), 'wb') as out:
                out.write(data)
        if not args.apply:
            return 'would_change', row
        try:
            thumb.upload_blob(data, overwrite=True, etag=props.etag, match_condition=MatchConditions.IfNotModified,
                              content_settings=ContentSettings(content_type='image/jpeg'), metadata={MARKER: '1'})
        except Exception as error:
            if getattr(error, 'status_code', None) == 412:
                return 'conflict', row
            raise
        return 'changed', row

    # Rows are handled in key order; --limit counts rows that needed work, so reruns and small batches are cheap.
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(pending), args.workers * 4):
            if args.limit and done >= args.limit:
                break
            for future in [pool.submit(process, row) for row in pending[start:start + args.workers * 4]]:
                try:
                    outcome, row = future.result()
                except Exception as error:
                    outcome, row = 'failed', {'RowKey': f'? ({type(error).__name__}: {error})'}
                counts[outcome] += 1
                if outcome in ('would_change', 'changed'):
                    done += 1
                if outcome in ('failed', 'no_frame', 'conflict'):
                    print(f"{outcome}: {row['RowKey']}")
    print(('APPLIED ' if args.apply else 'DRY RUN ') + ' '.join(f'{key}={value}' for key, value in counts.items()))
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--account', default=os.getenv('STORAGE_ACCOUNT_NAME', ''), help='storage account name')
    parser.add_argument('--library', required=True, help='library (user id) partition key')
    parser.add_argument('--apply', action='store_true', help='write the new thumbnails (default: dry run)')
    parser.add_argument('--modified-before', default='', help='ISO time; skip thumbnails written at/after it')
    parser.add_argument('--limit', type=int, default=0, help='stop after this many videos needing work (0 = all)')
    parser.add_argument('--after', default='', help='resume after this photo filename (RowKey)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--preview-dir', default='', help='also write the new JPEGs here')
    parser.add_argument('--ffmpeg', default='')
    args = parser.parse_args(argv)
    if not args.account:
        parser.error('--account or STORAGE_ACCOUNT_NAME is required')
    run(args)


if __name__ == '__main__':
    main()
