"""Real storage callers: incomplete captures, pointer safety and failure retries."""
import json

import pytest
from azure.core import MatchConditions

import people_embedding_incremental as people_delta
import storage_utils as storage
import tag_embedding_incremental as tag_delta
import vision_utils
from tests.test_people_embedding_incremental import seed_base
from tests.test_people_embedding_index import ctx as people_ctx, _FakeBlob, _seed_person
from tests.test_tag_embedding_incremental import ctx as tag_ctx, seed, bootstrap, manifest, manifest_key


class Capture:
    def __init__(self, flag):
        setattr(self, flag, True)
        self.consumed = False

    def __iter__(self):
        self.consumed = True
        raise AssertionError('incomplete capture must not be consumed')


@pytest.mark.parametrize('flag', ['has_more', 'truncated'])
@pytest.mark.parametrize('strict', [False, True])
def test_people_caller_rejects_capture_before_consumption_or_full_fallback(people_ctx, monkeypatch, flag, strict):
    seed_base(people_ctx)
    _, _, blobs = people_ctx
    before = dict(blobs.blobs)
    capture = Capture(flag)
    monkeypatch.setattr(storage, '_build_user_people_embedding_index_snapshot',
                        lambda *args: pytest.fail('incomplete capture cannot authorize full fallback'))
    assert storage.refresh_user_people_embedding_index(
        'u1', source_version='v2', affected_person_ids=capture, incremental_only=strict,
        previous_source_version='v1', model_version='model-A') is None
    assert not capture.consumed and blobs.blobs == before
    with pytest.raises(people_delta.Deferred):
        people_delta.bounded_ids(capture)
    assert not capture.consumed


@pytest.mark.parametrize('flag', ['has_more', 'truncated'])
def test_tag_caller_rejects_capture_before_consumption(tag_ctx, flag):
    table, blobs, _ = tag_ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = dict(blobs.blobs)
    capture = Capture(flag)
    assert storage.refresh_user_tag_embedding_index(
        'u', source_version='v2', affected_filenames=capture,
        previous_source_version='v1', model_version='clip-test') is None
    assert not capture.consumed and blobs.blobs == before
    with pytest.raises(tag_delta.Deferred):
        tag_delta.bounded_ids(capture)
    assert not capture.consumed


@pytest.mark.parametrize('kind', ['people', 'tag'])
@pytest.mark.parametrize('pointer', ['', None, 123, 'near-prefix', 'slash', 'suffix', 'oversized'])
def test_present_invalid_pointer_never_reads_legacy_alias(people_ctx, tag_ctx, monkeypatch, kind, pointer):
    if kind == 'people':
        _, _, blobs = people_ctx
        monkeypatch.setitem(storage._CTX, 'blob_service_client', blobs)
        seed_base(people_ctx)
        user, container = 'u1', 'people-embedding-index'
    else:
        table, blobs, _ = tag_ctx
        seed(table, 'a', ['dog'])
        bootstrap()
        user, container = 'u', 'tags'
    prefix = getattr(storage, f'_{kind}_embedding_index_blob_key')(user)
    key = container + '/' + getattr(storage, f'_{kind}_embedding_index_manifest_blob_name')(user)
    value = json.loads(blobs.blobs[key])
    alias = container + '/' + getattr(storage, f'_{kind}_embedding_index_npz_blob_name')(user)
    blobs.blobs[alias] = blobs.blobs[container + '/' + value['npzBlobName']]
    # An absent pointer alone permits a valid legacy snapshot.
    value.pop('npzBlobName')
    blobs.blobs[key] = json.dumps(value).encode()
    loader = getattr(storage, f'_load_{kind}_embedding_index_npz')
    assert loader(user).source_version == 'v1'
    pointer = {'near-prefix': prefix + 'other.npz', 'slash': prefix + '.path/a.npz',
               'suffix': prefix + '.json', 'oversized': prefix + '.' + 'x' * 1024 + '.npz'}.get(pointer, pointer)
    value['npzBlobName'] = pointer
    blobs.blobs[key] = json.dumps(value).encode()
    monkeypatch.setattr(storage, f'_{kind}_embedding_index_blob_client',
                        lambda name: pytest.fail('invalid pointer reached data client'))
    # Manifest loading itself still needs its client.
    monkeypatch.setattr(storage, f'_load_{kind}_embedding_index_manifest', lambda user: value)
    assert loader(user) is None


@pytest.mark.parametrize('failure', ['client', 'factory', 'properties', 'download', 'upload', 'cas'])
def test_tag_touch_real_latch_retries_and_preserves_provenance(tag_ctx, monkeypatch, failure):
    table, blobs, _ = tag_ctx
    seed(table, 'a', ['dog'])
    bootstrap()
    before = manifest(blobs)
    cache = {'source_version': 'v1'}
    storage._TAG_EMBEDDING_INDEX_CACHE['u'] = cache
    factory = storage._tag_embedding_index_blob_client
    with monkeypatch.context() as patch:
        if failure in ('client', 'factory'):
            def unavailable(name):
                if failure == 'factory':
                    raise TimeoutError('client unavailable')
                return None
            patch.setattr(storage, '_tag_embedding_index_blob_client', unavailable)
        else:
            method = {'properties': 'get_blob_properties', 'download': 'download_blob',
                      'upload': 'upload_blob', 'cas': 'upload_blob'}[failure]
            original = getattr(_FakeBlob, method)
            def fail(self, *args, **kwargs):
                if self._key == manifest_key():
                    if failure == 'cas':
                        newer = dict(before, sourceVersion='concurrent')
                        blobs.blobs[manifest_key()] = json.dumps(newer).encode()
                        return original(self, *args, **kwargs)
                    raise TimeoutError('manifest unavailable')
                return original(self, *args, **kwargs)
            patch.setattr(_FakeBlob, method, fail)
        assert storage.touch_user_tag_embedding_index_state('u') == ''
    assert ('u', 'tag_embedding') not in storage._INDEX_MANIFEST_DIRTY_FLAGS
    assert storage._TAG_EMBEDDING_INDEX_CACHE['u'] is cache
    assert storage._tag_embedding_index_blob_client is factory
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'wrong-role-model')
    assert storage.touch_user_tag_embedding_index_state('u')
    dirty = manifest(blobs)
    assert dirty['baseSourceVersion'] == ('concurrent' if failure == 'cas' else 'v1')
    for field in ('npzBlobName', 'contributionBlobName', 'embeddingVersion', 'schemaVersion'):
        assert dirty[field] == before[field]
    assert storage.touch_user_tag_embedding_index_state('u') == ''  # Successful dedup only.
    storage._clear_manifest_dirty_flag('u', 'tag_embedding')
    assert storage.touch_user_tag_embedding_index_state('u')
    assert manifest(blobs)['baseSourceVersion'] == dirty['baseSourceVersion']


@pytest.mark.parametrize('kind', ['people', 'tag'])
@pytest.mark.parametrize('failure', ['data', 'manifest', 'cas', 'data-client', 'manifest-client', 'properties'])
def test_default_full_failure_keeps_old_generation_and_retry_publishes_new(people_ctx, tag_ctx, monkeypatch, kind, failure):
    # Fixtures share storage context; install the selected blob service explicitly.
    if kind == 'people':
        _, _, blobs = people_ctx
        monkeypatch.setitem(storage._CTX, 'blob_service_client', blobs)
        seed_base(people_ctx)
        user, container, changed = 'u1', 'people-embedding-index', ['p1', 'p2']
        assert storage.refresh_user_people_embedding_index(
            user, source_version='v2', affected_person_ids=['p1'], incremental_only=True,
            previous_source_version='v1', model_version='model-A') is not None
        people, _, _ = people_ctx
        _seed_person(people, user, 'p1', ['f1'], [99], name='changed')
    else:
        table, blobs, _ = tag_ctx
        seed(table, 'a', ['dog'])
        bootstrap()
        user, container, changed = 'u', 'tags', ['cat']
        assert storage.refresh_user_tag_embedding_index(
            user, source_version='v2', affected_filenames=['a'],
            previous_source_version='v1', model_version='clip-test') is not None
        seed(table, 'a', changed)
    refresh = getattr(storage, f'refresh_user_{kind}_embedding_index')
    load = getattr(storage, f'_load_{kind}_embedding_index_npz')
    touch = getattr(storage, f'touch_user_{kind}_embedding_index_state')
    manifest_name = getattr(storage, f'_{kind}_embedding_index_manifest_blob_name')(user)
    key = container + '/' + manifest_name
    assert touch(user)
    old_manifest = blobs.blobs[key]
    old = json.loads(old_manifest)
    cache = getattr(storage, f'_{kind.upper()}_EMBEDDING_INDEX_CACHE')
    sentinel = {'source_version': 'v2'}
    cache[user] = sentinel
    original = _FakeBlob.upload_blob
    calls = []
    with monkeypatch.context() as patch:
        if failure in ('data-client', 'manifest-client'):
            factory = getattr(storage, f'_{kind}_embedding_index_blob_client')
            patch.setattr(storage, f'_{kind}_embedding_index_blob_client',
                          lambda name: None if name.endswith('.npz' if failure == 'data-client' else '.json') else factory(name))
        elif failure == 'properties':
            patch.setattr(_FakeBlob, 'get_blob_properties', lambda self: (_ for _ in ()).throw(TimeoutError('read failed')))
        else:
            def upload(self, data, **kwargs):
                calls.append((self._key, kwargs))
                if self._key.endswith('.npz') and failure == 'data':
                    raise IOError('data failed')
                if self._key == key and failure == 'manifest':
                    raise IOError('manifest failed')
                if self._key.endswith('.npz') and failure == 'cas':
                    newer = dict(old, sourceVersion='concurrent')
                    blobs.blobs[key] = json.dumps(newer).encode()
                return original(self, data, **kwargs)
            patch.setattr(_FakeBlob, 'upload_blob', upload)
        assert refresh(user, source_version='v3') is None
    assert cache[user] is sentinel
    assert load(user).source_version == 'v2'
    if failure != 'cas':
        assert blobs.blobs[key] == old_manifest
    with monkeypatch.context() as patch:
        def record(self, data, **kwargs):
            calls.append((self._key, kwargs))
            return original(self, data, **kwargs)
        patch.setattr(_FakeBlob, 'upload_blob', record)
        assert refresh(user, source_version='v3') is not None
    committed = json.loads(blobs.blobs[key])
    assert committed['npzBlobName'] != old['npzBlobName']
    assert committed['sourceVersion'] == 'v3' and not committed['dirty']
    assert 'baseSourceVersion' not in committed and 'contributionBlobName' not in committed
    assert load(user).source_version == 'v3'
    if kind == 'tag':
        assert load(user).tags == changed
        assert storage.get_user_tag_embedding_index(user, allow_refresh=False)['tags'] == changed
    else:
        assert load(user).people_meta[0]['name'] == 'changed'
    assert calls[-1][0] == key
    assert calls[-1][1]['match_condition'] == MatchConditions.IfNotModified
    assert calls[-2][1]['overwrite'] is False


@pytest.mark.parametrize('kind', ['people', 'tag'])
def test_full_build_touch_during_scan_is_not_deduplicated_or_overwritten(people_ctx, tag_ctx, monkeypatch, kind):
    if kind == 'people':
        _, _, blobs = people_ctx
        monkeypatch.setitem(storage._CTX, 'blob_service_client', blobs)
        seed_base(people_ctx)
        user = 'u1'
    else:
        table, _, _ = tag_ctx
        seed(table, 'a', ['dog'])
        bootstrap()
        user = 'u'
    touch = getattr(storage, f'touch_user_{kind}_embedding_index_state')
    assert touch(user)
    builder_name = f'_build_user_{kind}_embedding_index_snapshot'
    build = getattr(storage, builder_name)
    def race(*args, **kwargs):
        result = build(*args, **kwargs)
        assert touch(user)  # Full builder must re-arm the latch before capture.
        return result
    monkeypatch.setattr(storage, builder_name, race)
    assert getattr(storage, f'refresh_user_{kind}_embedding_index')(user, source_version='v3') is None
    assert getattr(storage, f'_load_{kind}_embedding_index_manifest')(user)['dirty']
    assert getattr(storage, f'_load_{kind}_embedding_index_npz')(user).source_version == 'v1'


def test_default_tag_generation_retains_base_and_model_on_dirty_touch(tag_ctx, monkeypatch):
    table, blobs, _ = tag_ctx
    seed(table, 'a', ['dog'])
    assert storage.refresh_user_tag_embedding_index('u', source_version='full-v1') is not None
    before = manifest(blobs)
    assert 'contributionBlobName' not in before
    monkeypatch.setattr(vision_utils, 'get_text_embedding_version', lambda: 'non-owner-model')
    assert storage.touch_user_tag_embedding_index_state('u')
    storage._clear_manifest_dirty_flag('u', 'tag_embedding')
    assert storage.touch_user_tag_embedding_index_state('u')
    dirty = manifest(blobs)
    assert dirty['baseSourceVersion'] == 'full-v1'
    assert dirty['npzBlobName'] == before['npzBlobName']
    assert dirty['embeddingVersion'] == 'clip-test'


def test_full_people_old_propertiesless_fake_still_replaces_incremental_pointer(people_ctx, monkeypatch):
    seed_base(people_ctx)
    assert storage.refresh_user_people_embedding_index(
        'u1', source_version='v2', affected_person_ids=['p1'], incremental_only=True,
        previous_source_version='v1', model_version='model-A') is not None
    before = storage._load_people_embedding_index_manifest('u1')
    monkeypatch.setattr(_FakeBlob, 'get_blob_properties', None)
    assert storage.refresh_user_people_embedding_index('u1', source_version='v3') is not None
    after = storage._load_people_embedding_index_manifest('u1')
    assert after['npzBlobName'] != before['npzBlobName']
    assert storage._load_people_embedding_index_npz('u1').source_version == 'v3'