"""Coverage for storage_utils._warm_face_crops_for_photo: background
pre-generation of face cover-crop blobs right after detection, instead of
routes/people.py's face_crop() generating each one lazily on first People-page
view -- see the 2026-09-29 forenkla-qa HAR investigation (opening a person
with thousands of never-before-viewed faces took 4+ minutes because every
crop's download+decode+crop+upload cycle ran synchronously, one request at a
time, on first view).
"""
from __future__ import annotations

import json

import pytest

import storage_utils
from tests.fakes import FakeTable


class _ResourceNotFoundError(Exception):
    pass


@pytest.fixture
def ctx(monkeypatch):
    faces = FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', faces)
    return faces


def _seed_face(faces: FakeTable, user_id: str, face_id: str, **overrides) -> None:
    faces.upsert_entity({
        'PartitionKey': user_id,
        'RowKey': face_id,
        'bbox': json.dumps({'left': 10, 'top': 10, 'width': 40, 'height': 40}),
        'imageWidth': 200,
        'imageHeight': 200,
        **overrides,
    })


def test_warm_generates_and_uploads_crop_for_new_face(ctx, monkeypatch):
    faces = ctx
    _seed_face(faces, 'u1', 'face-1')

    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda kind, blob: (_ for _ in ()).throw(_ResourceNotFoundError()))
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda kind, blob, content, content_type: uploaded.append((kind, blob, content, content_type)))
    monkeypatch.setattr(storage_utils, 'crop_face_thumbnail', lambda image_bytes, filename, bbox, img_w, img_h, rotation: b'cropped-jpeg-bytes')

    calls = []

    def get_image_bytes():
        calls.append(1)
        return b'original-bytes'

    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-1'], get_image_bytes, rotation=0)

    assert len(uploaded) == 1
    assert uploaded[0][0] == 'cover'
    assert uploaded[0][2] == b'cropped-jpeg-bytes'
    assert len(calls) == 1


def test_warm_skips_face_with_existing_cached_crop(ctx, monkeypatch):
    faces = ctx
    _seed_face(faces, 'u1', 'face-1')

    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda kind, blob: {'size': 123})
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a, **k: uploaded.append(a))

    def get_image_bytes():
        raise AssertionError('should never download when the crop is already cached')

    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-1'], get_image_bytes, rotation=0)

    assert uploaded == []


def test_warm_downloads_original_at_most_once_for_multiple_faces(ctx, monkeypatch):
    faces = ctx
    _seed_face(faces, 'u1', 'face-1')
    _seed_face(faces, 'u1', 'face-2')
    _seed_face(faces, 'u1', 'face-3')

    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda kind, blob: (_ for _ in ()).throw(_ResourceNotFoundError()))
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda kind, blob, content, content_type: uploaded.append(blob))
    monkeypatch.setattr(storage_utils, 'crop_face_thumbnail', lambda *a, **k: b'cropped')

    calls = []

    def get_image_bytes():
        calls.append(1)
        return b'original-bytes'

    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-1', 'face-2', 'face-3'], get_image_bytes, rotation=0)

    assert len(uploaded) == 3
    assert len(calls) == 1, 'multi-face photo must download the original at most once, not once per face'


def test_warm_never_raises_on_missing_face_entity(ctx):
    # face-id with no corresponding table row (e.g. deleted between detection
    # and this background pass running) -- must be skipped silently.
    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-does-not-exist'], lambda: b'x', rotation=0)


def test_warm_never_raises_when_crop_generation_fails(ctx, monkeypatch):
    faces = ctx
    _seed_face(faces, 'u1', 'face-1')
    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda kind, blob: (_ for _ in ()).throw(_ResourceNotFoundError()))
    monkeypatch.setattr(storage_utils, 'crop_face_thumbnail', lambda *a, **k: None)
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a, **k: uploaded.append(a))

    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-1'], lambda: b'original-bytes', rotation=0)

    assert uploaded == []


def test_warm_is_noop_with_no_face_ids_or_no_face_table(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    # Should return immediately without touching get_image_bytes.
    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', [], lambda: (_ for _ in ()).throw(AssertionError('unused')), rotation=0)
    storage_utils._warm_face_crops_for_photo('u1', 'a.jpg', ['face-1'], lambda: (_ for _ in ()).throw(AssertionError('unused')), rotation=0)
