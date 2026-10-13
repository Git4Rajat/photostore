"""Opt-in, task-scoped RGB reuse; processors never share images across photos.

The yielded RGB image is read-only to consumers. Each job owns at most one
decoded image and releases it on source changes and on every exit path.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import io

from PIL import Image, ImageOps

_JOB = ContextVar('ipwork_image_job', default=None)


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
