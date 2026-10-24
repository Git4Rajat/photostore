"""Verified, immutable JPEG derivatives. Browser SAS grants only staging writes.

The metadata result CAS publishes the pointer; an abandoned attempt can leave
unreferenced blobs but cannot replace a published derivative via its staging SAS.
"""
import hashlib
import io
import json

from PIL import Image
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContentSettings

LIMITS = {'thumbnail': (512_000, 120), 'preview': (1_500_000, 2048)}


def staging_name(library, photo, revision, attempt, kind):
    if kind not in LIMITS or not all(isinstance(v, str) and v for v in (library, photo, revision, attempt)):
        raise ValueError('Derivative staging requires a complete processing identity')
    key = hashlib.sha256(json.dumps([library, photo, revision, attempt], separators=(',', ':')).encode()).hexdigest()
    scope = hashlib.sha256(library.encode()).hexdigest()
    return f'processing-staging/v1/{scope}/{key}/{kind}.jpg'


def published_name(library, photo, revision, kind, content_hash):
    key = hashlib.sha256(json.dumps([library, photo, revision], separators=(',', ':')).encode()).hexdigest()
    scope = hashlib.sha256(library.encode()).hexdigest()
    return f'derived/v1/{scope}/{key}/{kind}/{content_hash}.jpg'


def _jpeg(data, kind):
    cap, edge = LIMITS[kind]
    if not 0 < len(data) <= cap:
        raise ValueError('Derivative exceeds byte limit or is empty')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG' or max(image.size) > edge or min(image.size) <= 0:
            raise ValueError('Derivative is not a bounded JPEG')
        image.load()
        return image.size


def _read(blob, kind):
    props = blob.get_blob_properties()
    size = int(props.size)
    if not 0 < size <= LIMITS[kind][0] or props.content_settings.content_type != 'image/jpeg':
        raise ValueError('Invalid derivative blob size or content type')
    data = blob.download_blob(offset=0, length=size, etag=props.etag,
                              match_condition=MatchConditions.IfNotModified).readall()
    if len(data) != size:
        raise ValueError('Derivative changed while being read')
    _jpeg(data, kind)
    return data, props


def publish(storage, metadata, kind, *, data=None, staged=False):
    from processing_fence import current_fence
    fence = current_fence(metadata['PartitionKey'], metadata['RowKey'])
    if fence is None:
        raise ValueError('Immutable derivative publication requires a processing fence')
    fence.check()
    container = storage._CTX.get('blob_thumbnail_container')
    if not container:
        raise ValueError('Derivative storage unavailable')
    if staged:
        source = storage._get_blob_client(container, staging_name(fence.library, fence.photo, fence.revision, fence.attempt, kind))
        data, _ = _read(source, kind)
    if not isinstance(data, bytes):
        raise ValueError('Derivative bytes required')
    width, height = _jpeg(data, kind)
    content_hash = hashlib.sha256(data).hexdigest()
    name = published_name(fence.library, fence.photo, fence.revision, kind, content_hash)
    blob = storage._get_blob_client(container, name)
    if blob is None:
        raise ValueError('Derivative storage unavailable')
    fence.check()
    try:
        blob.upload_blob(data, overwrite=False, content_settings=ContentSettings(content_type='image/jpeg'))
    except ResourceExistsError:
        pass  # Retry must prove exact stored content, not assume existence means success.
    actual, props = _read(blob, kind)
    if actual != data:
        raise ValueError('Immutable derivative content mismatch')
    fence.check()
    receipt = dict(schemaVersion=1, library=fence.library, photoId=fence.photo, revision=fence.revision,
        attempt=fence.attempt, kind=kind, blob=name, sha256=content_hash, size=len(data),
        width=width, height=height, etag=str(props.etag))
    metadata[kind + 'Artifact'] = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
    return receipt


def promote_prepared(storage, metadata):
    """Qualify browser-prepared JPEGs before a worker publishes completion.

    Upload finalization can mark the shared preparation blobs done without an
    attempt receipt. Read bounded, ETag-pinned bytes, then publish a create-only
    copy under the current source fence. The caller commits the new pointers
    with its normal result CAS; never overwrite an existing receipt or blob.
    """
    from image_utils import is_video_file
    from processing_fence import current_fence
    fence = current_fence(metadata['PartitionKey'], metadata['RowKey'])
    if fence is None:
        raise ValueError('Prepared derivative promotion requires a processing fence')
    fence.check()
    if is_video_file(metadata['RowKey']):
        return  # Video preparation remains browser-owned.
    for kind in ('thumbnail', 'preview'):
        if metadata.get(kind + '_status') != 'done' or metadata.get(kind + 'Artifact'):
            continue
        name = fence.photo if kind == 'thumbnail' else f'preview/{fence.photo}.jpg'
        blob = storage._get_blob_client(storage._CTX['blob_thumbnail_container'], name)
        data, _ = _read(blob, kind)
        publish(storage, metadata, kind, data=data)


def derivative_blob(metadata, kind, fallback):
    raw = (metadata or {}).get(kind + 'Artifact')
    if not raw:
        return fallback
    receipt = json.loads(raw)
    if (receipt.get('schemaVersion') != 1 or receipt.get('kind') != kind
            or receipt.get('photoId') != metadata.get('anonymousImageId')
            or receipt.get('blob') != published_name(receipt['library'], receipt['photoId'],
                receipt['revision'], kind, receipt['sha256'])
            or (metadata.get('PartitionKey') and receipt.get('library') != metadata['PartitionKey'])):
        raise ValueError('Invalid derivative pointer')
    # Serving can retain a previous generation for this photo while new work runs.
    # Completion qualification separately requires the exact current revision.
    return receipt['blob']


def validate_receipt(metadata, kind, *, current_revision=True):
    name = derivative_blob(metadata, kind, None)
    if not name:
        raise ValueError('Verified derivative receipt required')
    receipt = json.loads(metadata[kind + 'Artifact'])
    if (current_revision and receipt.get('revision') != metadata.get('processingRevision')
            or not isinstance(receipt.get('sha256'), str) or len(receipt['sha256']) != 64
            or any(char not in '0123456789abcdef' for char in receipt['sha256'])
            or type(receipt.get('size')) is not int or not 0 < receipt['size'] <= LIMITS[kind][0]
            or any(type(receipt.get(key)) is not int or not 0 < receipt[key] <= LIMITS[kind][1]
                   for key in ('width', 'height'))
            or not isinstance(receipt.get('etag'), str) or receipt['etag'] in ('', '*')
            or not isinstance(receipt.get('attempt'), str) or not receipt['attempt']):
        raise ValueError('Invalid or stale derivative receipt')
    return receipt


def verify_receipt(storage, metadata, kind):
    receipt = validate_receipt(metadata, kind)
    blob = storage._get_blob_client(storage._CTX['blob_thumbnail_container'], receipt['blob'])
    data, props = _read(blob, kind)
    if (str(props.etag) != receipt['etag'] or hashlib.sha256(data).hexdigest() != receipt['sha256']
            or len(data) != receipt['size'] or _jpeg(data, kind) != (receipt['width'], receipt['height'])):
        raise ValueError('Published derivative no longer matches its receipt')
    return receipt
