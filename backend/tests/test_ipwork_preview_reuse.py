import base64
import io

import pytest
from PIL import Image

import app


@pytest.fixture
def preview_ctx(monkeypatch):
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, 'JPEG')
    preview = stream.getvalue()
    calls, metrics = [], []
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setattr(app, 'IPWORK_SHARED_DECODE', True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {
        'anonymousImageId': 'physical.CR3', 'preview_status': 'done'})
    monkeypatch.setattr(app, 'get_media_properties', lambda *_: {'size': 10_000_000})
    def download(kind, blob):
        calls.append((kind, blob))
        return preview if kind == 'thumbnail' else b'original-with-exif'
    monkeypatch.setattr(app, 'download_media_bytes', download)
    monkeypatch.setattr(app, '_ipwork_metric', lambda name, **kwargs: metrics.append((name, kwargs)))
    for step in ('ocr', 'face', 'ai_vision', 'exif', 'map_detection'):
        monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, step, lambda _, __, data: {'hasData': True, 'received': data})
    return preview, calls, metrics


def test_ready_preview_avoids_original_download(preview_ctx):
    preview, calls, metrics = preview_ctx
    result = app._run_ipwork_steps('user', 'display.CR3', ['ocr', 'face', 'ai_vision'])
    assert all(step['received'] == preview for step in result.values())
    assert calls == [('thumbnail', app._preview_cache_blob_name('physical.CR3'))]
    assert app._ipwork_metrics_context.source_bytes == 10_000_000
    assert ('preview_download_bytes', {'count': len(preview)}) in metrics
    assert not any(name == 'source_download_bytes' for name, _ in metrics)


@pytest.mark.parametrize('reason', ['missing', 'corrupt', 'truncated', 'unknown_size'])
def test_unusable_preview_falls_back(monkeypatch, preview_ctx, reason):
    preview, calls, metrics = preview_ctx
    def download(kind, blob):
        calls.append((kind, blob))
        if kind == 'image':
            return b'original-with-exif'
        if reason == 'missing':
            raise FileNotFoundError()
        return preview[:10] if reason == 'truncated' else b'bad'
    monkeypatch.setattr(app, 'download_media_bytes', download)
    if reason == 'unknown_size':
        monkeypatch.setattr(app, 'get_media_properties', lambda *_: {'size': 0})
    result = app._run_ipwork_steps('user', 'display.CR3', ['ocr', 'face'])
    assert result['ocr']['received'] == result['face']['received'] == b'original-with-exif'
    assert sum(kind == 'image' for kind, _ in calls) == 1
    assert sum(name == 'preview_reuse_failed' for name, _ in metrics) == 1


def test_metadata_steps_receive_original_after_reused_preview(preview_ctx):
    preview, calls, _ = preview_ctx
    result = app._run_ipwork_steps('user', 'display.CR3', ['ocr', 'exif', 'map_detection', 'face'])
    assert result['ocr']['received'] == result['face']['received'] == preview
    assert result['exif']['received'] == result['map_detection']['received'] == b'original-with-exif'
    assert sum(kind == 'image' for kind, _ in calls) == 1


def test_generated_preview_keeps_original_for_metadata(monkeypatch, preview_ctx):
    preview, calls, _ = preview_ctx
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'preview', lambda *_: {
        'hasData': True, 'data': base64.b64encode(preview).decode()})
    result = app._run_ipwork_steps('user', 'display.CR3', ['preview', 'ocr', 'exif'])
    assert result['ocr']['received'] == preview
    assert result['exif']['received'] == b'original-with-exif'
    assert calls == [('image', 'physical.CR3')]


@pytest.mark.parametrize('filename,status,enabled', [
    ('video.mp4', 'done', True), ('a.CR3', 'running', True), ('a.CR3', 'done', False)])
def test_preview_requires_enabled_ready_photo(monkeypatch, preview_ctx, filename, status, enabled):
    _, calls, _ = preview_ctx
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', enabled)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'preview_status': status})
    result = app._run_ipwork_steps('user', filename, ['ocr'])
    assert result['ocr']['received'] == b'original-with-exif'
    assert calls == [('image', filename)]
