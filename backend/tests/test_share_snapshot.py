"""Share snapshot: what gets written, what is skipped, and what is removed again."""
import json
from html import unescape
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import share_snapshot as ss
import share_snapshot_page as page


class ExistsError(Exception):
    pass


class FakeContainer:
    def __init__(self):
        self.blobs = {}   # name -> (bytes, metadata, content_settings)
        self.uploads = []

    def upload_blob(self, name, data, overwrite=True, metadata=None, content_settings=None):
        if not overwrite and name in self.blobs:
            raise ExistsError('BlobAlreadyExists')
        self.uploads.append(name)
        self.blobs[name] = (data, dict(metadata or {}), content_settings)

    def list_blobs(self, name_starts_with='', include=None):
        return [SimpleNamespace(name=n, metadata=m[1]) for n, m in sorted(self.blobs.items()) if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        if name not in self.blobs:
            raise KeyError('BlobNotFound')
        del self.blobs[name]

    def download_blob(self, name):
        return SimpleNamespace(readall=lambda: self.blobs[name][0])


def photo(name, version='v1', video=False, preview=b'P', thumb=b'T', rot=0):
    return ss.Photo(name, version=version, is_video=video, rotation=rot,
                    read_thumbnail=lambda: thumb, read_preview=None if video else (lambda: preview))


def entity(**kw):
    base = {'PartitionKey': 'u', 'RowKey': 'a', 'isPublic': True, 'publicToken': 'tok', 'accessCode': '', 'publicExpiresAt': ''}
    base.update(kw)
    return base


META = {'title': 'Trip', 'description': '3 photos shared on Keepsake.', 'image': 'https://spa/og-image.png', 'image_is_fallback': 'true'}


def build(container, photos, total=None, ent=None):
    return ss.build(ent or entity(), container, photos, total if total is not None else len(photos), META,
                    'https://archive.example', 'https://web.example')


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('SHARE_SNAPSHOT_WEB_URL', 'https://acct.z6.web.core.windows.net/')


def test_eligibility_excludes_codes_expiry_and_private_albums():
    assert ss.eligible(entity())
    assert not ss.eligible(entity(isPublic=False))
    assert not ss.eligible(entity(publicToken=''))
    assert not ss.eligible(entity(accessCode='scrypt$hash'))
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert not ss.eligible(entity(publicExpiresAt=past))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert ss.eligible(entity(publicExpiresAt=future))


def test_disabled_without_a_web_url(monkeypatch):
    monkeypatch.delenv('SHARE_SNAPSHOT_WEB_URL')
    assert not ss.enabled()
    assert ss.public_url({'shareSnapshotToken': 'tok', 'shareSnapshotAt': 'x'}, 'tok') == ''


def test_public_url_only_once_built_for_this_token():
    assert ss.public_url(entity(), 'tok') == ''
    assert ss.public_url(entity(shareSnapshotToken='old', shareSnapshotAt='x'), 'tok') == ''
    assert ss.public_url(entity(shareSnapshotToken='tok', shareSnapshotAt='x'), 'tok') == \
        'https://acct.z6.web.core.windows.net/s/tok/'


def test_build_writes_assets_then_manifest_then_page():
    c = FakeContainer()
    result = build(c, [photo('a.jpg'), photo('b.jpg')])
    assert result == {'cached': 2, 'total': 2, 'removed': 0}
    assert set(c.blobs) == {f's/tok/{n}' for n in ('t/0.jpg', 'p/0.jpg', 't/1.jpg', 'p/1.jpg', 'manifest.json', 'index.html')}
    assert c.uploads[-2:] == ['s/tok/manifest.json', 's/tok/index.html']
    manifest = json.loads(c.blobs['s/tok/manifest.json'][0])
    assert manifest['total'] == 2 and manifest['cached'] == 2
    assert manifest['photos'][0] == {'n': 'a.jpg', 'rot': 0, 'trot': 0, 'video': False, 't': 't/0.jpg', 'p': 'p/0.jpg'}


def test_videos_get_a_thumbnail_but_no_preview():
    c = FakeContainer()
    build(c, [photo('v.mp4', video=True)])
    assert 's/tok/t/0.jpg' in c.blobs and 's/tok/p/0.jpg' not in c.blobs
    entry = json.loads(c.blobs['s/tok/manifest.json'][0])['photos'][0]
    assert entry['video'] and entry['p'] == ''


def test_only_the_first_limit_photos_are_cached_but_total_is_kept(monkeypatch):
    monkeypatch.setattr(ss, 'PREVIEW_LIMIT', 2)
    c = FakeContainer()
    build(c, [photo(f'{i}.jpg') for i in range(5)], total=5)
    manifest = json.loads(c.blobs['s/tok/manifest.json'][0])
    assert manifest['cached'] == 2 and manifest['total'] == 5
    assert 's/tok/p/2.jpg' not in c.blobs


def test_unchanged_photos_are_not_read_or_uploaded_again():
    c = FakeContainer()
    build(c, [photo('a.jpg')])
    c.uploads.clear()
    reads = []
    again = ss.Photo('a.jpg', version='v1', read_thumbnail=lambda: reads.append('t'), read_preview=lambda: reads.append('p'))
    build(c, [again])
    assert reads == []
    assert c.uploads == ['s/tok/manifest.json', 's/tok/index.html']


def test_changed_version_rewrites_only_that_photo():
    c = FakeContainer()
    build(c, [photo('a.jpg'), photo('b.jpg')])
    c.uploads.clear()
    build(c, [photo('a.jpg'), photo('b.jpg', version='v2')])
    assert sorted(u for u in c.uploads if '/t/' in u or '/p/' in u) == ['s/tok/p/1.jpg', 's/tok/t/1.jpg']


def test_files_for_photos_that_left_the_album_are_removed():
    c = FakeContainer()
    build(c, [photo('a.jpg'), photo('b.jpg')])
    result = build(c, [photo('a.jpg')])
    assert result['removed'] == 2
    assert 's/tok/t/1.jpg' not in c.blobs and 's/tok/p/1.jpg' not in c.blobs


def test_oversized_or_failing_previews_fall_back_to_the_thumbnail():
    c = FakeContainer()
    big = ss.Photo('big.jpg', version='v', read_thumbnail=lambda: b'T', read_preview=lambda: b'x' * (ss.PREVIEW_MAX_BYTES + 1))

    def boom():
        raise RuntimeError('decode failed')
    bad = ss.Photo('bad.jpg', version='v', read_thumbnail=lambda: b'T', read_preview=boom)
    build(c, [big, bad])
    entries = json.loads(c.blobs['s/tok/manifest.json'][0])['photos']
    assert [(e['t'], e['p']) for e in entries] == [('t/0.jpg', ''), ('t/1.jpg', '')]


def test_delete_removes_everything_under_the_token_and_nothing_else():
    c = FakeContainer()
    build(c, [photo('a.jpg')])
    c.blobs['s/other/index.html'] = (b'x', {}, None)
    assert ss.delete(c, 'tok') == 4
    assert list(c.blobs) == ['s/other/index.html']


def test_revoke_page_takes_down_the_entry_points_first():
    c = FakeContainer()
    build(c, [photo('a.jpg')])
    ss.revoke_page(c, 'tok')
    assert 's/tok/index.html' not in c.blobs and 's/tok/manifest.json' not in c.blobs
    assert 's/tok/t/0.jpg' in c.blobs


def test_sweep_removes_only_expired_snapshots():
    c = FakeContainer()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    build(c, [photo('a.jpg')], ent=entity(publicToken='old', publicExpiresAt=past))
    build(c, [photo('a.jpg')], ent=entity(publicToken='live', publicExpiresAt=future))
    build(c, [photo('a.jpg')], ent=entity(publicToken='forever'))
    assert ss.sweep_expired(c) == ['old']
    assert not any(n.startswith('s/old/') for n in c.blobs)
    assert any(n.startswith('s/live/') for n in c.blobs) and any(n.startswith('s/forever/') for n in c.blobs)


def test_page_is_self_contained_with_a_hash_pinned_csp():
    html = page.render({'title': 'A <b>&</b> B', 'description': 'd'}, token='tok', spa_url='https://web.example/x',
                       archive_url='https://archive.example', image='https://i/p.jpg')
    assert '<b>' not in html.split('<title>')[1].split('</title>')[0]
    csp = unescape(html.split('Content-Security-Policy" content="')[1].split('" />')[0])
    assert "script-src 'self' 'sha256-" in csp and "style-src 'self' 'sha256-" in csp
    assert "connect-src 'self' https://web.example https://archive.example" in csp
    assert 'unsafe-inline' not in csp and 'unsafe-eval' not in csp
    script = html.split('<script>')[1].split('</script>')[0]
    assert page._hash(script) in csp
    style = html.split('<style>')[1].split('</style>')[0]
    assert page._hash(style) in csp
    assert '"token": "tok"' in script and '"spa": "https://web.example"' in script
    assert 'og:image" content="https://i/p.jpg"' in html


def test_page_embeds_config_safely():
    html = page.render({'title': 't'}, token='</script><script>alert(1)', spa_url='https://w', archive_url='https://a', image='')
    assert '</script><script>alert(1)' not in html


def test_page_offers_download_of_the_album_and_of_each_photo():
    html = page.render({'title': 't'}, token='tok', spa_url='https://w', archive_url='https://a.example', image='')
    assert 'id="dlAll"' in html and 'id="lbDl"' in html
    script = html.split('<script>')[1].split('</script>')[0]
    assert "'/public/albums/'" in script and "'/public/photos/'" in script and "'/image/'" in script


def test_static_link_is_not_handed_out_once_a_code_is_added():
    built = {'shareSnapshotToken': 'tok', 'shareSnapshotAt': 'x'}
    assert ss.public_url(entity(**built), 'tok')
    assert ss.public_url(entity(accessCode='scrypt$hash', **built), 'tok') == ''


def test_shell_publishes_page_and_an_empty_manifest_without_assets():
    c = FakeContainer()
    assert ss.write_shell(entity(), c, META, 7, 'https://archive.example', 'https://web.example')
    assert set(c.blobs) == {'s/tok/manifest.json', 's/tok/index.html'}
    manifest = json.loads(c.blobs['s/tok/manifest.json'][0])
    assert manifest['building'] is True and manifest['total'] == 7 and manifest['photos'] == []


def test_shell_never_overwrites_a_finished_build_and_the_build_replaces_the_shell():
    c = FakeContainer()
    build(c, [photo('a.jpg')])
    assert ss.write_shell(entity(), c, META, 1, 'https://archive.example', 'https://web.example')
    assert 'building' not in json.loads(c.blobs['s/tok/manifest.json'][0])
    c2 = FakeContainer()
    ss.write_shell(entity(), c2, META, 1, 'https://archive.example', 'https://web.example')
    build(c2, [photo('a.jpg')])
    done = json.loads(c2.blobs['s/tok/manifest.json'][0])
    assert 'building' not in done and done['cached'] == 1


def test_page_polls_for_the_build_and_hands_over_while_it_runs():
    html = page.render({'title': 't'}, token='tok', spa_url='https://w', archive_url='https://a', image='')
    script = html.split('<script>')[1].split('</script>')[0]
    assert 'm.building' in script and 'waitForBuild' in script


def test_page_handles_failures_gracefully():
    html = page.render({'title': 't'}, token='tok', spa_url='https://w', archive_url='https://a', image='')
    script = html.split('<script>')[1].split('</script>')[0]
    assert 'img.onerror = function () { b.remove(); }' in script      # a missing thumbnail leaves no broken tile
    assert 'This album has no photos yet.' in script
    assert '120000' in script                                          # a stuck build hands over to the real app
    assert '<noscript>' in html and 'https://w/public/album/tok' in html


def test_ensure_app_copies_the_bundle_once_and_points_current_at_it(monkeypatch):
    monkeypatch.setattr(ss, '_app_checked_at', 0.0)
    served = {'/static-album/app-files.json': json.dumps({'version': 'v9', 'entry': 'assets/m.js', 'css': ['assets/m.css'],
                                                          'files': ['assets/m.js', 'assets/m.css']}).encode(),
              '/static-album/assets/m.js': b'js', '/static-album/assets/m.css': b'css'}
    calls = []

    def fetch(url):
        calls.append(url)
        return served['/' + url.split('//', 1)[1].split('/', 1)[1]]
    c = FakeContainer()
    assert ss.ensure_app(c, 'https://web.example', fetch) == 'v9'
    assert set(c.blobs) == {'app/v9/assets/m.js', 'app/v9/assets/m.css', 'app/current.json'}
    assert json.loads(c.blobs['app/current.json'][0]) == {'version': 'v9', 'entry': 'assets/m.js', 'css': ['assets/m.css']}
    n = len(calls)
    assert ss.ensure_app(c, 'https://web.example', fetch) == 'v9' and len(calls) == n   # cached, no refetch


def test_ensure_app_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(ss, '_app_checked_at', 0.0)

    def boom(url):
        raise OSError('web app down')
    assert ss.ensure_app(FakeContainer(), 'https://web.example', boom) == ''


def test_page_prefers_the_real_ui_bundle_and_falls_back_to_plain_view():
    html = page.render({'title': 't'}, token='tok', spa_url='https://w', archive_url='https://a', image='')
    script = html.split('<script>')[1].split('</script>')[0]
    assert "/app/current.json" in script and 's.onerror = function () { root.remove(); legacy(); }' in script
    assert '.catch(legacy)' in script


def test_delete_ignores_azure_style_missing_blob(caplog):
    class ResourceNotFoundError(Exception):
        pass

    class Container:
        def delete_blob(self, name):
            raise ResourceNotFoundError('The specified blob does not exist.\nErrorCode:BlobNotFound')

    with caplog.at_level('WARNING'):
        ss._delete(Container(), 's/x/index.html')
    assert not caplog.records
