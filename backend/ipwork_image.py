"""Opt-in, task-scoped RGB reuse; processors never share images across photos.

The yielded RGB image is read-only to consumers. Each job owns at most one
decoded image and releases it on source changes and on every exit path.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import io

from PIL import Image, ImageOps

_JOB = ContextVar('ipwork_image_job', default=None)
_CROP_INPUT = ContextVar('ipwork_crop_input', default=None)
_ORIGINAL_INPUT = ContextVar('ipwork_original_input', default=None)


@contextmanager
def original_input_job(user, filename):
    job = {'user': user, 'filename': filename, 'original': None}
    token = _ORIGINAL_INPUT.set(job)
    try:
        yield
    finally:
        job['original'] = None
        _ORIGINAL_INPUT.reset(token)


def _original_binding(user, filename, metadata):
    if (not isinstance(metadata, dict) or metadata.get('PartitionKey') != user
            or metadata.get('RowKey') != filename):
        return None
    blob = str(metadata.get('anonymousImageId') or '')
    source_hash = str(metadata.get('fileHash') or '')
    size = metadata.get('size')
    if (not (blob or source_hash) or not isinstance(size, int)
            or isinstance(size, bool) or size <= 0):
        return None
    return blob, source_hash, size


def remember_original_input(user, filename, metadata, data):
    """Hand off the existing immutable buffer, not another copy or a shared cache."""
    job = _ORIGINAL_INPUT.get()
    binding = _original_binding(user, filename, metadata)
    if (job is not None and (job['user'], job['filename']) == (user, filename)
            and binding is not None and isinstance(data, bytes) and len(data) == binding[2]):
        job['original'] = (binding, data)


def original_input(user, filename, metadata):
    job = _ORIGINAL_INPUT.get()
    if job is None or (job['user'], job['filename']) != (user, filename):
        return None
    original = job['original']
    if original is not None and original[0] == _original_binding(user, filename, metadata):
        return original[1]
    return None


@dataclass(frozen=True)
class CropPreview:
    data: bytes
    source_blob: str
    source_hash: str
    source_revision: str = ''

    def __call__(self):
        return self.data


@contextmanager
def crop_input_job(user, filename):
    token = _CROP_INPUT.set({'user': user, 'filename': filename, 'preview': None})
    try:
        yield
    finally:
        _CROP_INPUT.reset(token)


def remember_crop_preview(user, filename, metadata, data, width, height):
    """Retain only immutable bytes that the caller has fully decoded/validated."""
    job = _CROP_INPUT.get()
    if (job is None or (job['user'], job['filename']) != (user, filename)
            or not isinstance(data, bytes) or len(data) > 1_500_000
            or not data.startswith(b'\xff\xd8') or not (0 < width <= 2048 and 0 < height <= 2048)):
        return
    blob = str(metadata.get('anonymousImageId') or '')
    source_hash = str(metadata.get('fileHash') or '')
    # Legacy mutable filename-only blobs have no stable source identity.
    if blob or source_hash:
        job['preview'] = CropPreview(data, blob, source_hash, str(metadata.get('processingRevision') or ''))


def crop_preview(user, filename, metadata):
    job = _CROP_INPUT.get()
    if job is None or (job['user'], job['filename']) != (user, filename):
        return None
    preview = job['preview']
    if (preview is not None and preview.source_blob == str(metadata.get('anonymousImageId') or '')
            and preview.source_hash == str(metadata.get('fileHash') or '')):
        return preview
    return None


class _Images:
    def __init__(self):
        self.source = None
        self.filename = None
        self.image = None

    def close(self):
        if self.image is not None:
            self.image.close()
        self.source = self.filename = self.image = None


@contextmanager
def image_job(enabled=False):
    images = _Images() if enabled else None
    token = _JOB.set(images)
    try:
        yield
    finally:
        if images is not None:
            images.close()
        _JOB.reset(token)


def sharing_enabled():
    return _JOB.get() is not None


def _decode(source, filename, prepare):
    data = prepare(source, filename) if prepare else source
    with Image.open(io.BytesIO(data)) as opened:
        oriented = ImageOps.exif_transpose(opened)
        try:
            return oriented.convert('RGB')
        finally:
            oriented.close()


@contextmanager
def rgb_image(source, filename='', prepare=None):
    images = _JOB.get()
    if images is None:
        image = _decode(source, filename, prepare)
        try:
            yield image
        finally:
            image.close()
        return
    if images.source is not source or images.filename != filename:
        images.close()
        image = _decode(source, filename, prepare)
        images.source, images.filename, images.image = source, filename, image
    yield images.image
