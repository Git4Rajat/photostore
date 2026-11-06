"""Public album routes: visibility rules, access-code gate, paging, media gating and zip download."""
import io
import zipfile

import pytest

import app
import password_auth


class FakeAlbumStore:
    def __init__(self, names):
        self.names = list(names)

    def page(self, entity, offset, limit):
        return self.names[offset:offset + limit], len(self.names)

    def contains(self, entity, name):
        return name in self.names

    def all_names(self, entity):
        return list(self.names)


@pytest.fixture
def share(monkeypatch):
    state = {
        'entity': {'PartitionKey': 'owner', 'name': 'Trip', 'isPublic': True},
        'meta': {},
        'throttle_ok': True,
        'failures': [],
        'blobs': {},
    }
    monkeypatch.setattr(app, '_find_public_album_by_token', lambda token: state['entity'] if token == 'tok' else None)
    monkeypatch.setattr(app, 'album_store', FakeAlbumStore(['a.jpg', 'b.jpg', 'gone.jpg']))
    monkeypatch.setattr(app, '_get_metadata_entities',
                        lambda owner, names: {n: state['meta'].get(n, {}) for n in names})
    monkeypatch.setattr(app, '_validate_media_filename', lambda n: n if n.endswith('.jpg') else None)
    monkeypatch.setattr(app, '_public_photo_urls',
                        lambda token, name, blob_name=None, thumbnail_blob=None: {'url': f'/img/{name}', 'thumbnailUrl': f'/th/{name}'})
    monkeypatch.setattr(app, 'resolve_physical_blob_name', lambda owner, name, kind: 'blob-' + name)
    monkeypatch.setattr(password_auth, 'login_attempt_allowed', lambda table, row_key=None: state['throttle_ok'])
    monkeypatch.setattr(password_auth, 'record_login_failure', lambda table, row_key=None, **k: state['failures'].append(row_key))
    monkeypatch.setattr(password_auth, 'record_login_success', lambda table, row_key=None: None)
    monkeypatch.setattr(password_auth, 'seconds_until_unlocked', lambda table, row_key=None: 30)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    state['client'] = app.app.test_client()
    return state


@pytest.mark.parametrize('mutate', [
    lambda e: e.update(isPublic=False),
    lambda e: e.update(expiresAt='2000-01-01T00:00:00+00:00'),
])
def test_private_or_expired_albums_are_404_everywhere(share, monkeypatch, mutate):
    mutate(share['entity'])
    monkeypatch.setattr(app, '_album_is_expired', lambda e: bool(e.get('expiresAt')))
    client = share['client']
    assert client.get('/api/public/albums/tok').status_code == 404
    assert client.get('/public/albums/tok/download-check').status_code == 404
    assert client.post('/public/albums/tok/download', json={}).status_code == 404
    assert client.get('/public/photos/tok/thumbnail/a.jpg').status_code == 404
    assert client.get('/public/photos/tok/image/a.jpg').status_code == 404


def test_unknown_token_is_404(share):
    assert share['client'].get('/api/public/albums/nope').json == {'error': 'Album not found'}


class TestPublicAlbum:
    def test_lists_photos_skipping_trashed_ones(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        share['meta']['gone.jpg'] = {'processing_state': 'deleted'}
        body = share['client'].get('/api/public/albums/tok').json
        assert [p['filename'] for p in body['photos']] == ['a.jpg', 'b.jpg']
        assert body['album'] == {'name': 'Trip', 'photoCount': 3} and body['total'] == 3 and body['hasMore'] is False
        assert body['photos'][0]['url'] == '/img/a.jpg'

    def test_paging_clamps_and_reports_has_more(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        body = share['client'].get('/api/public/albums/tok?offset=-5&limit=1').json
        assert body['offset'] == 0 and len(body['photos']) == 1 and body['hasMore'] is True
        body = share['client'].post('/api/public/albums/tok', json={'offset': 2, 'limit': 9999}).json
        assert [p['filename'] for p in body['photos']] == ['gone.jpg'] and body['hasMore'] is False

    def test_bad_paging_values_are_400(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        assert share['client'].get('/api/public/albums/tok?limit=abc').status_code == 400

    def test_open_album_sets_no_grant_cookie(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        assert 'Set-Cookie' not in share['client'].get('/api/public/albums/tok').headers


class TestAccessCode:
    @pytest.fixture(autouse=True)
    def protected(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        share['entity']['accessCode'] = 'sesame'

    def test_missing_code_asks_for_one_without_counting_a_failure(self, share):
        response = share['client'].get('/api/public/albums/tok')
        assert response.status_code == 401 and response.json['codeRequired'] is True
        assert share['failures'] == []

    def test_wrong_code_is_rejected_and_counted_against_the_token(self, share):
        response = share['client'].post('/api/public/albums/tok', json={'accessCode': 'wrong'})
        assert response.status_code == 401
        assert share['failures'] == ['album-code-throttle:tok']

    def test_locked_out_album_returns_429_with_retry_hint(self, share):
        share['throttle_ok'] = False
        response = share['client'].post('/api/public/albums/tok', json={'accessCode': 'sesame'})
        assert response.status_code == 429 and response.json['retryAfterSeconds'] == 30

    def test_right_code_returns_photos_and_a_grant_that_unlocks_media(self, share, monkeypatch):
        client = share['client']
        response = client.post('/api/public/albums/tok', json={'accessCode': ' sesame '})
        assert response.status_code == 200 and len(response.json['photos']) == 3
        cookie = response.headers['Set-Cookie']
        assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
        monkeypatch.setattr(app, 'blob_service_client', object())
        monkeypatch.setattr(app, 'get_media_properties', lambda kind, blob: {'content_type': 'image/png', 'size': 3})
        monkeypatch.setattr(app, '_stream_media_response', lambda *a, **k: app.Response(b'png', mimetype='image/png'))
        assert client.get('/public/photos/tok/image/a.jpg').status_code == 200

    def test_media_is_404_without_the_grant(self, share, monkeypatch):
        monkeypatch.setattr(app, '_stream_media_response', lambda *a, **k: pytest.fail('served without grant'))
        monkeypatch.setattr(app, 'blob_service_client', object())
        client = share['client']
        assert client.get('/public/photos/tok/image/a.jpg').status_code == 404
        assert client.get('/public/photos/tok/thumbnail/a.jpg').status_code == 404

    def test_download_needs_the_code_when_there_is_no_grant(self, share):
        assert share['client'].post('/public/albums/tok/download', data={}).status_code == 401

    def test_download_accepts_get_and_applies_the_same_code_gate(self, share):
        # A browser that blocks the download popup can turn the form submission into a GET.
        assert share['client'].get('/public/albums/tok/download?filenames=%5B%22a.jpg%22%5D').status_code == 401


class TestPublicMedia:
    @pytest.fixture(autouse=True)
    def open_album(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        monkeypatch.setattr(app, 'blob_service_client', object())
        monkeypatch.setattr(app, 'get_media_properties', lambda kind, blob: {'content_type': 'image/webp', 'size': 7})
        self.streamed = []
        monkeypatch.setattr(app, '_stream_media_response',
                            lambda kind, blob, **kw: self.streamed.append((kind, blob, kw)) or app.Response(b'x', mimetype=kw['content_type']))

    def test_rejects_invalid_names_and_photos_outside_the_album(self, share):
        client = share['client']
        assert client.get('/public/photos/tok/image/notes.txt').status_code == 400
        assert client.get('/public/photos/tok/thumbnail/notes.txt').status_code == 400
        assert client.get('/public/photos/tok/image/other.jpg').status_code == 404
        assert client.get('/public/photos/tok/thumbnail/other.jpg').status_code == 404
        assert self.streamed == []

    def test_image_streams_the_physical_blob_as_a_download(self, share):
        assert share['client'].get('/public/photos/tok/image/a.jpg').status_code == 200
        kind, blob, kw = self.streamed[0]
        assert (kind, blob) == ('image', 'blob-a.jpg')
        assert kw['download_filename'] == 'a.jpg' and kw['content_type'] == 'image/webp'

    def test_thumbnail_service_unconfigured_is_503(self, share, monkeypatch):
        monkeypatch.setattr(app, 'blob_service_client', None)
        assert share['client'].get('/public/photos/tok/thumbnail/a.jpg').status_code == 503

    def test_missing_thumbnail_blob_falls_back_to_the_uncached_placeholder(self, share, monkeypatch):
        monkeypatch.setattr(app, 'placeholder_bytes', b'placeholder')
        monkeypatch.setattr(app, '_is_missing_media_error', lambda exc: True)

        def missing(*a, **k):
            raise FileNotFoundError('no blob')

        monkeypatch.setattr(app, 'get_media_properties', missing)
        response = share['client'].get('/public/photos/tok/thumbnail/a.jpg')
        assert response.status_code == 200 and response.data == b'placeholder'
        assert 'no-store' in response.headers['Cache-Control']

    def test_image_stream_failures_map_to_404_or_500(self, share, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError('storage')

        monkeypatch.setattr(app, '_stream_media_response', boom)
        monkeypatch.setattr(app, '_is_missing_media_error', lambda exc: False)
        assert share['client'].get('/public/photos/tok/image/a.jpg').status_code == 500
        monkeypatch.setattr(app, '_is_missing_media_error', lambda exc: True)
        assert share['client'].get('/public/photos/tok/image/a.jpg').status_code == 404


class TestDownload:
    @pytest.fixture(autouse=True)
    def open_album(self, share, monkeypatch):
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        self.blobs = {'blob-a.jpg': b'AAA', 'blob-b.jpg': b'BBB'}

        def download(kind, blob):
            return self.blobs[blob]

        monkeypatch.setattr(app, 'download_media_bytes', download)

    def names(self, response):
        return sorted(zipfile.ZipFile(io.BytesIO(response.data)).namelist())

    def test_download_check_is_ok_for_a_live_album(self, share):
        assert share['client'].get('/public/albums/tok/download-check').json == {'ok': True}

    def test_zips_the_selected_photos_only(self, share):
        response = share['client'].post('/public/albums/tok/download', data={'filenames': '["a.jpg", "intruder.jpg"]'})
        assert response.status_code == 200 and response.mimetype == 'application/zip'
        assert self.names(response) == ['a.jpg']
        assert response.headers['Content-Disposition'] == 'attachment; filename=public-album-tok.zip'
        assert response.headers['Cache-Control'] == 'no-store'

    def test_comma_separated_and_json_list_bodies_work(self, share):
        csv = share['client'].post('/public/albums/tok/download', data={'filenames': 'b.jpg, a.jpg'})
        assert self.names(csv) == ['a.jpg', 'b.jpg']
        listed = share['client'].post('/public/albums/tok/download', json={'filenames': ['b.jpg']})
        assert self.names(listed) == ['b.jpg']

    def test_selection_outside_the_album_falls_back_to_the_whole_album_and_skips_unreadable_photos(self, share):
        response = share['client'].post('/public/albums/tok/download', json={'filenames': ['intruder.jpg']})
        assert self.names(response) == ['a.jpg', 'b.jpg']  # gone.jpg has no blob and is skipped

    def test_nothing_readable_is_404(self, share):
        self.blobs.clear()
        response = share['client'].post('/public/albums/tok/download', json={})
        assert response.status_code == 404 and response.json['error'] == 'No files could be downloaded'
