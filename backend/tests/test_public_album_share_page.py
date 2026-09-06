"""Unit tests for the /public/album/<token> crawler-facing share page.

Public album links (created via AlbumsPage "Share album") are pasted into
iMessage/WhatsApp/Slack, whose link-unfurler bots fetch the URL without
running JS. The SPA is a static single-page app, so previously every route
served the same static index.html with fixed "Keepsake" OG tags -- bots never
saw the actual album name/photo. This page returns real per-album OG tags for
the bot, then meta-refreshes real browsers into the interactive SPA viewer.
Access-code-protected albums must stay fully generic here since a bot can
never supply the code.
"""
from __future__ import annotations

import app


def _entity(**overrides):
    base = {
        'PartitionKey': 'owner-1',
        'RowKey': 'album-1',
        'name': 'Beach Trip 2026',
        'isPublic': True,
        'filenames': '["IMG_0001.jpg", "IMG_0002.jpg"]',
        'publicExpiresAt': '',
        'accessCode': '',
    }
    base.update(overrides)
    return base


def test_meta_for_open_album_uses_real_name_and_full_size_photo(monkeypatch):
    monkeypatch.setattr(app, 'SPA_BASE_URL', 'https://app.example.com')
    monkeypatch.setattr(app, '_get_metadata_entity', lambda owner_id, name: {})

    with app.app.test_request_context('/public/album/tok123'):
        meta = app._public_album_share_meta(_entity(), 'tok123')

    assert meta['title'] == 'Beach Trip 2026'
    assert '2 photo' in meta['description']
    # Uses the full-size photo, not the 120x120 thumbnail -- WhatsApp/Facebook
    # silently drop link-preview images below ~200x200. Falls back to the
    # relative proxy image route when no blob/SAS client is configured (as in
    # this test environment); it must still be made absolute against the
    # backend's own host, not left relative.
    assert meta['image'].startswith('http')
    assert '/public/photos/tok123/image/IMG_0001.jpg' in meta['image']
    assert not meta['image_is_fallback']


def test_meta_for_locked_album_stays_generic_even_with_real_name(monkeypatch):
    monkeypatch.setattr(app, 'SPA_BASE_URL', 'https://app.example.com')

    with app.app.test_request_context('/public/album/tok123'):
        meta = app._public_album_share_meta(_entity(accessCode='1234'), 'tok123')

    assert meta['title'] == 'Shared album (locked)'
    assert 'Beach Trip' not in meta['title']
    assert 'Beach Trip' not in meta['description']
    assert meta['image'] == 'https://app.example.com/og-image.png'
    assert meta['image_is_fallback']


def test_meta_for_missing_or_unpublished_album_is_generic(monkeypatch):
    monkeypatch.setattr(app, 'SPA_BASE_URL', 'https://app.example.com')

    with app.app.test_request_context('/public/album/tok123'):
        missing = app._public_album_share_meta(None, 'tok123')
        unpublished = app._public_album_share_meta(_entity(isPublic=False), 'tok123')
        expired = app._public_album_share_meta(_entity(publicExpiresAt='2020-01-01T00:00:00+00:00'), 'tok123')

    for meta in (missing, unpublished, expired):
        assert meta['title'] == 'Shared album'
        assert meta['image_is_fallback']


def test_render_escapes_album_name_against_injection():
    meta = {
        'title': '<script>alert(1)</script>',
        'description': 'desc',
        'image': 'https://app.example.com/og-image.png',
        'image_is_fallback': 'true',
    }
    page = app._render_public_album_share_page(meta, 'https://app.example.com/public/album/tok123')

    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    # Fixed-size dimensions are only advertised for the known-size fallback image.
    assert 'og:image:width' in page


def test_render_omits_dimensions_for_real_photo_thumbnail():
    meta = {
        'title': 'Beach Trip 2026',
        'description': '2 photos shared on Keepsake.',
        'image': 'https://storage.example.com/thumb.jpg',
        'image_is_fallback': '',
    }
    page = app._render_public_album_share_page(meta, 'https://app.example.com/public/album/tok123')

    assert 'og:image:width' not in page


def test_share_page_route_returns_html_with_meta_refresh(monkeypatch):
    monkeypatch.setattr(app, 'SPA_BASE_URL', 'https://app.example.com')
    monkeypatch.setattr(app, '_find_public_album_by_token', lambda token: _entity())
    monkeypatch.setattr(app, '_get_metadata_entity', lambda owner_id, name: {})

    client = app.app.test_client()
    resp = client.get('/public/album/tok123')

    assert resp.status_code == 200
    assert resp.content_type.startswith('text/html')
    body = resp.get_data(as_text=True)
    assert 'Beach Trip 2026' in body
    assert 'https://app.example.com/public/album/tok123' in body
    assert resp.headers['Cache-Control'] == 'no-store'


def test_share_page_route_handles_unknown_token_generically(monkeypatch):
    monkeypatch.setattr(app, 'SPA_BASE_URL', 'https://app.example.com')
    monkeypatch.setattr(app, '_find_public_album_by_token', lambda token: None)

    client = app.app.test_client()
    resp = client.get('/public/album/does-not-exist')

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Shared album' in body
    assert 'https://app.example.com/public/album/does-not-exist' in body


def test_public_url_in_album_payload_points_at_backend_share_page(monkeypatch):
    """_album_entity_to_payload feeds AlbumsPage's copy-to-clipboard link --
    it must point at this backend's own share page, not straight at the SPA,
    or bots will keep seeing generic Keepsake tags."""
    with app.app.test_request_context('/albums', base_url='https://backend.example.com'):
        payload = app._album_entity_to_payload(_entity(publicToken='tok123'))

    assert payload['publicUrl'] == 'https://backend.example.com/public/album/tok123'
