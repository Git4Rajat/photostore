"""Shared albums get the sharp grid-tile image too: live page payload, proxy route and static snapshot."""
import json
from types import SimpleNamespace

import pytest

import app
import grid_images
from tests.test_public_album_route_behaviour import FakeAlbumStore, share  # noqa: F401 (fixture)


def _stamped(physical):
    return {'anonymousImageId': physical,
            'gridImage': grid_images.record(grid_images.blob_name(physical), 600, 400, 30000, f'preview/{physical}.jpg')}


# ---- live page ---------------------------------------------------------------


def test_album_payload_lists_a_grid_url_only_for_photos_that_have_one(share, monkeypatch):
    monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
    share['meta']['a.jpg'] = _stamped('id-a')
    monkeypatch.setattr(app, '_public_photo_urls', lambda token, name, blob_name=None, thumbnail_blob=None, grid_blob=None: {
        'url': f'/img/{name}', 'thumbnailUrl': f'/th/{name}', 'gridUrl': f'https://sas/{grid_blob}' if grid_blob else ''})
    photos = share['client'].get('/api/public/albums/tok').json['photos']
    assert photos[0]['gridUrl'] == 'https://sas/grid/id-a.webp'
    assert 'gridUrl' not in photos[1]


def test_grid_urls_are_signed_like_thumbnails_with_a_proxy_fallback(monkeypatch):
    def sign(container, blob, download_filename=None):
        if blob.startswith('grid/') and container == 'broken':
            raise RuntimeError('no signing key')
        return f'https://sas/{container}/{blob}', None
    monkeypatch.setattr(app, '_create_stable_read_sas_url', sign)
    urls = app._public_photo_urls('tok', 'a.jpg', blob_name='id-a', grid_blob='grid/id-a.webp')
    assert urls['gridUrl'] == f'https://sas/{app.BLOB_THUMBNAIL_CONTAINER}/grid/id-a.webp'
    assert app._public_photo_urls('tok', 'a.jpg', blob_name='id-a')['gridUrl'] == ''
    monkeypatch.setattr(app, 'BLOB_THUMBNAIL_CONTAINER', 'broken')
    assert app._public_photo_urls('tok', 'a.jpg', blob_name='id-a', grid_blob='grid/id-a.webp')['gridUrl'] == \
        '/public/photos/tok/grid/a.jpg'


@pytest.fixture
def media(share, monkeypatch):
    streamed = []
    monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
    monkeypatch.setattr(app, '_album_grant_valid', lambda entity, token: True)
    monkeypatch.setattr(app, '_validate_media_filename', lambda n: n if n.endswith('.jpg') else None)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda owner, name: share['meta'].get(name))
    monkeypatch.setattr(app, 'blob_service_client', object())
    monkeypatch.setattr(app, 'get_media_properties', lambda kind, blob: {'content_type': 'image/webp', 'size': 3})

    def stream(kind, blob, **kwargs):
        streamed.append((kind, blob, kwargs['content_type']))
        return app.Response(b'GRD', mimetype=kwargs['content_type'])
    monkeypatch.setattr(app, '_stream_media_response', stream)
    return SimpleNamespace(client=share['client'], meta=share['meta'], streamed=streamed)


def test_grid_route_serves_the_album_photos_grid_image(media):
    media.meta['a.jpg'] = _stamped('id-a')
    response = media.client.get('/public/photos/tok/grid/a.jpg')
    assert response.status_code == 200 and response.data == b'GRD'
    assert media.streamed == [('thumbnail', 'grid/id-a.webp', 'image/webp')]


def test_grid_route_404s_without_a_grid_image_or_outside_the_album(media):
    media.meta['a.jpg'] = {'anonymousImageId': 'id-a'}
    assert media.client.get('/public/photos/tok/grid/a.jpg').status_code == 404
    media.meta['other.jpg'] = _stamped('id-o')
    assert media.client.get('/public/photos/tok/grid/other.jpg').status_code == 404
    assert media.client.get('/public/photos/nope/grid/a.jpg').status_code == 404
    assert media.streamed == []


def test_grid_route_honours_the_access_code_grant(media, monkeypatch):
    media.meta['a.jpg'] = _stamped('id-a')
    monkeypatch.setattr(app, '_album_grant_valid', lambda entity, token: False)
    assert media.client.get('/public/photos/tok/grid/a.jpg').status_code == 404


# ---- static snapshot -----------------------------------------------------------


def test_snapshot_photos_read_the_grid_image(monkeypatch):
    monkeypatch.setattr(app, 'album_store', FakeAlbumStore(['a.jpg', 'b.jpg', 'v.mp4']))
    meta = {'a.jpg': _stamped('id-a'), 'b.jpg': {'anonymousImageId': 'id-b'}, 'v.mp4': _stamped('id-v')}
    monkeypatch.setattr(app, '_get_metadata_entities', lambda owner, names: {n: meta[n] for n in names})
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, blob: f'{kind}:{blob}'.encode())
    photos, total = app._share_snapshot_photos({'PartitionKey': 'owner'})
    assert total == 3
    assert photos[0].read_grid() == b'thumbnail:grid/id-a.webp'
    assert photos[1].read_grid() is None
    assert photos[2].read_grid is None   # videos have no grid image


class _Albums:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query_entities(self, query, select=None):
        self.queries.append((query, select))
        return iter(self.rows)


def test_refresh_rebuilds_only_snapshots_that_already_exist(monkeypatch):
    monkeypatch.setenv('SHARE_SNAPSHOT_WEB_URL', 'https://acct.z6.web.core.windows.net/')
    base = {'PartitionKey': 'lib', 'isPublic': True, 'publicToken': 'tok', 'shareSnapshotToken': 'tok'}
    albums = _Albums([
        dict(base, RowKey='live'),
        dict(base, RowKey='never-built', shareSnapshotToken=''),
        dict(base, RowKey='old-token', shareSnapshotToken='old'),
        dict(base, RowKey='coded', accessCode='scrypt$x'),
        dict(base, RowKey='deleted', deleted=True),
        dict(base, RowKey='private', isPublic=False),
    ])
    queued = []
    monkeypatch.setattr(app, 'albums_table_client', albums)
    monkeypatch.setattr(app, '_enqueue_share_snapshot', lambda action, user_id, album_id, delay=0: queued.append((action, user_id, album_id)) or True)
    assert app._refresh_share_snapshots('lib') == 1
    assert queued == [('build', 'lib', 'live')]
    assert albums.queries[0][0] == "PartitionKey eq 'lib'"


def test_refresh_is_off_without_snapshots(monkeypatch):
    monkeypatch.delenv('SHARE_SNAPSHOT_WEB_URL', raising=False)
    monkeypatch.setattr(app, 'albums_table_client', _Albums([{'RowKey': 'x'}]))
    assert app._refresh_share_snapshots('lib') == 0


# ---- grid job hands over to the snapshots ----------------------------------------


def _rows(n):
    return [{'PartitionKey': 'lib', 'RowKey': f'IMG_{i}.jpg', 'anonymousImageId': f'id{i}', 'preview_status': 'done',
             'uploadDate': 'x'} for i in range(n)]


@pytest.fixture
def job(monkeypatch):
    state = SimpleNamespace(sent=[], refreshed=[], made=True)
    monkeypatch.setattr(app, 'clustering_queue_client', SimpleNamespace(send_message=lambda body: state.sent.append(json.loads(body))))
    monkeypatch.setattr(app, 'metadata_table_client', object())
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda user_id, **kw: iter(_rows(3)))
    monkeypatch.setattr(app, '_make_grid_image', lambda user_id, row: state.made)
    monkeypatch.setattr(app, '_refresh_share_snapshots', lambda user_id: state.refreshed.append(user_id) or 1)
    return state


def test_grid_job_chain_carries_its_count_and_refreshes_snapshots_at_the_end(job, monkeypatch):
    monkeypatch.setattr(grid_images, 'BATCH_SIZE', 2)
    app._run_grid_image_job({'user_id': 'lib', 'made': 5})
    assert job.sent[0]['made'] == 7 and job.refreshed == []   # more to do: no refresh yet
    monkeypatch.setattr(grid_images, 'BATCH_SIZE', 10)
    app._run_grid_image_job(job.sent[0])
    assert job.refreshed == ['lib']


def test_grid_job_that_made_nothing_leaves_snapshots_alone(job):
    job.made = False
    app._run_grid_image_job({'user_id': 'lib', 'reason': 'wave'})
    assert job.refreshed == [] and job.sent == []


@pytest.mark.parametrize('reason', ['admin_backfill', 'script_backfill'])
def test_a_manual_backfill_always_refreshes_snapshots(job, reason):
    job.made = False   # every photo already had its grid image
    app._run_grid_image_job({'user_id': 'lib', 'reason': reason})
    assert job.refreshed == ['lib']
