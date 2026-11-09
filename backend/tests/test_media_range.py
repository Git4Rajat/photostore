import pytest

import app


@pytest.mark.parametrize('header,total,expected', [
    ('bytes=0-99', 1000, (0, 99)),
    ('bytes=900-', 1000, (900, 999)),
    ('bytes=900-5000', 1000, (900, 999)),
    ('bytes=-100', 1000, (900, 999)),
    ('bytes=-5000', 1000, (0, 999)),
    ('bytes=1000-', 1000, 'unsatisfiable'),
    ('bytes=50-10', 1000, 'unsatisfiable'),
    ('bytes=-0', 1000, 'unsatisfiable'),
    ('bytes=0-1,5-9', 1000, None),
    ('items=0-1', 1000, None),
    ('bytes=-', 1000, None),
])
def test_parse_byte_range(header, total, expected):
    assert app._parse_byte_range(header, total) == expected


def test_media_content_type_repairs_generic_video_types():
    assert app._media_content_type('application/octet-stream', 'a.mov') == 'video/quicktime'
    assert app._media_content_type(None, 'a.mp4') == 'video/mp4'
    assert app._media_content_type('video/webm', 'a.webm') == 'video/webm'
    assert app._media_content_type(None, 'a.jpg') == 'image/jpeg'


class _Downloader:
    def __init__(self, data):
        self.data = data

    def chunks(self):
        yield self.data


class _Blob:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def download_blob(self, offset=None, length=None, max_concurrency=1):
        self.calls.append((offset, length))
        if offset is None:
            return _Downloader(self.data)
        return _Downloader(self.data[offset:offset + length])


class _Service:
    def __init__(self, blob):
        self.blob = blob

    def get_blob_client(self, container, blob):
        return self.blob


@pytest.fixture
def blob(monkeypatch):
    b = _Blob(bytes(range(256)) * 4)
    monkeypatch.setattr(app, 'blob_service_client', _Service(b))
    monkeypatch.setattr(app, '_media_container_for_kind', lambda kind: 'images')
    return b


def _serve(headers=None):
    with app.app.test_request_context('/x', headers=headers or {}):
        resp = app._stream_media_response('image', 'b', content_type='video/mp4',
                                          cache_control='private', content_length=1024)
        return resp, resp.get_data()


def test_full_response_advertises_ranges(blob):
    resp, body = _serve()
    assert resp.status_code == 200 and len(body) == 1024
    assert resp.headers['Accept-Ranges'] == 'bytes'


def test_range_request_returns_206_slice(blob):
    resp, body = _serve({'Range': 'bytes=10-19'})
    assert resp.status_code == 206
    assert body == blob.data[10:20]
    assert resp.headers['Content-Range'] == 'bytes 10-19/1024'
    assert resp.headers['Content-Length'] == '10'
    assert blob.calls == [(10, 10)]


def test_open_ended_range_and_unsatisfiable(blob):
    resp, body = _serve({'Range': 'bytes=1000-'})
    assert resp.status_code == 206 and len(body) == 24
    resp, _ = _serve({'Range': 'bytes=2000-'})
    assert resp.status_code == 416
    assert resp.headers['Content-Range'] == 'bytes */1024'
