"""Regression test for proxy_thumbnail()'s fallback to proxy_preview().

Both view functions used to live in app.py; the modularity pass (2026-09-15)
moved them into routes/photos.py as a Blueprint. The automated extraction
rewrote every reference to a shared app.py name as app.<name> (needed for
things that genuinely stayed in app.py), but proxy_thumbnail's direct call to
its sibling proxy_preview -- now in the *same* file, not app.py -- got
rewritten the same way, producing app.proxy_preview(...), which no longer
exists and would raise AttributeError the first time a thumbnail-missing,
backend-preview-required file was requested. Fixed to a bare call. This test
pins the fix so a future refactor can't silently reintroduce it.
"""
from __future__ import annotations

import app
import routes.photos as photos_module
from routes.photos import proxy_thumbnail


def test_thumbnail_miss_falls_back_to_the_real_proxy_preview_function(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_validate_media_filename', lambda name: name)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a, **k: {'RowKey': 'photo.cr3'})
    monkeypatch.setattr(app, '_resolve_media_blob_name', lambda *a, **k: 'photo.cr3')

    def _raise_not_found(*a, **k):
        raise RuntimeError('404 ResourceNotFound: blob does not exist')

    monkeypatch.setattr(app, 'get_media_properties', _raise_not_found)
    monkeypatch.setattr(app, '_filename_requires_backend_preview', lambda name: True)

    sentinel = object()
    called_with = {}

    def _fake_proxy_preview(filename):
        called_with['filename'] = filename
        return sentinel

    # Patched on the module the function actually looks its name up in
    # (proxy_thumbnail.__globals__ is routes.photos.__dict__) -- this is
    # exactly what would have failed to matter under the app.proxy_preview
    # bug, since that path never reaches this module's own namespace at all.
    monkeypatch.setattr(photos_module, 'proxy_preview', _fake_proxy_preview)

    with app.app.test_request_context('/api/photos/thumbnail/photo.cr3'):
        result = proxy_thumbnail('photo.cr3')

    assert result is sentinel
    assert called_with['filename'] == 'photo.cr3'
