"""Unit tests for ipwork_vision.py's RAW-decode routing.

See test_ipwork_face.py's matching tests for the full context: PIL has no
generic codec for RAW containers (e.g. CR3), so vision_utils.encode_image_embedding's
Image.open() raised 'cannot identify image file' on every RAW upload before
this fix -- confirmed live against a real production backlog (173/173 failed
ai_vision jobs were CR3, 0 were any other format). _decodable_image_bytes must
route RAW extensions through image_utils.extract_raw_preview_bytes (the same
helper ipwork_thumbnail.py already uses) before anything touches PIL.
"""
from __future__ import annotations

import ipwork_vision


def test_decodable_image_bytes_passes_through_non_raw_unchanged():
    original = b'plain-jpeg-bytes'
    assert ipwork_vision._decodable_image_bytes(original, 'photo.jpg') == original


def test_decodable_image_bytes_extracts_preview_for_raw_extension(monkeypatch):
    calls = []

    def fake_extract(image_bytes, filename):
        calls.append((image_bytes, filename))
        return b'decodable-jpeg-preview'

    monkeypatch.setattr(ipwork_vision, 'extract_raw_preview_bytes', fake_extract)

    result = ipwork_vision._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_0036.cr3')

    assert result == b'decodable-jpeg-preview'
    assert calls == [(b'raw-cr3-bytes', 'IMG_0036.cr3')]


def test_decodable_image_bytes_falls_back_to_original_when_extraction_fails(monkeypatch):
    monkeypatch.setattr(ipwork_vision, 'extract_raw_preview_bytes', lambda image_bytes, filename: None)

    result = ipwork_vision._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_0036.cr3')

    assert result == b'raw-cr3-bytes'


def test_process_vision_passes_decoded_bytes_to_encoder(monkeypatch):
    import numpy as np

    monkeypatch.setattr(ipwork_vision.vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(ipwork_vision, '_load_vocabulary', lambda: True)
    monkeypatch.setattr(ipwork_vision, '_vocab_labels', ['cat', 'dog'])
    monkeypatch.setattr(ipwork_vision, '_vocab_embeddings', np.eye(2, dtype='float32'))
    monkeypatch.setattr(ipwork_vision.vision_utils, 'get_text_embedding_version', lambda: 'test-version')

    monkeypatch.setattr(ipwork_vision, 'extract_raw_preview_bytes', lambda image_bytes, filename: b'decoded-preview')

    received = {}

    def fake_encode(image_bytes):
        received['bytes'] = image_bytes
        return [1.0, 0.0]

    monkeypatch.setattr(ipwork_vision.vision_utils, 'encode_image_embedding', fake_encode)

    result = ipwork_vision.process_vision('lib-A', 'IMG_0036.cr3', b'raw-cr3-bytes')

    assert received['bytes'] == b'decoded-preview'
    assert result['hasData'] is True


def test_shared_decode_failure_preserves_processor_failure_shape(monkeypatch):
    from ipwork_image import image_job
    monkeypatch.setattr(ipwork_vision.vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(ipwork_vision, '_load_vocabulary', lambda: True)
    with image_job(True):
        result = ipwork_vision.process_vision('lib-A', 'broken.jpg', b'not-an-image')
    # Deterministic on these bytes -- distinct from 'image_embedding_failed'
    # (decode succeeded but the model produced nothing usable), which keeps
    # retrying.
    assert result == {'hasData': False, 'error': 'image_undecodable'}


def test_clip_reports_preprocess_lock_and_inference_without_changing_output(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import vision_utils as vision
    import processing_metrics as metrics
    class Tensor:
        def unsqueeze(self, dim): return self
        def to(self, device): return self
        def norm(self, **kwargs): return self
        def __truediv__(self, other): return self
        def squeeze(self, dim): return self
        def cpu(self): return self
        def tolist(self): return [1.0, 0.0]
    tensor = Tensor()
    monkeypatch.setattr(vision, '_load_model', lambda: True)
    monkeypatch.setattr(vision, '_device', lambda: 'cpu')
    monkeypatch.setattr(vision, '_PREPROCESS', lambda image: tensor)
    monkeypatch.setattr(vision, '_MODEL', SimpleNamespace(encode_image=lambda pixels: tensor))
    monkeypatch.setattr(vision, 'torch', SimpleNamespace(no_grad=nullcontext))
    with metrics.accounting() as counter:
        assert vision.encode_rgb_image_embedding(object()) == [1.0, 0.0]
    assert {'clip_preprocess_ms', 'clip_lock_wait_ms', 'clip_inference_ms'} <= counter.values.keys()
