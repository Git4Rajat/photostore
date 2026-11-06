"""Bounded lexical merges: compatible payloads, explicit inputs, atomic CAS."""
import gzip
import json
from types import SimpleNamespace
import tracemalloc

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
import pytest

from index_files import RowsWriter, iter_rows
import lexical_incremental as inc
import storage_utils as su
from tests.fakes import FakeTable


class Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data

    def readinto(self, stream):
        for offset in range(0, len(self.data), inc.CHUNK):
            stream.write(self.data[offset:offset + inc.CHUNK])


class Blob:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def get_blob_properties(self):
        if self.name not in self.service.data:
            raise ResourceNotFoundError(self.name)
        data = self.service.data[self.name]
        return SimpleNamespace(size=len(data), etag=str(hash(data)))

    def check(self, options):
        if 'etag' in options:
            assert options['match_condition'] == MatchConditions.IfNotModified
            if options['etag'] != self.get_blob_properties().etag:
                raise ResourceModifiedError('CAS conflict')

    def download_blob(self, **options):
        self.check(options)
        self.get_blob_properties()
        return Download(self.service.data[self.name])

    def upload_blob(self, data, overwrite=True, **options):
        self.check(options)
        if not overwrite and self.name in self.service.data:
            raise ResourceExistsError(self.name)
        self.service.data[self.name] = data.read() if hasattr(data, 'read') else data


class Service:
    def __init__(self):
        self.data = {}

    def get_blob_client(self, container, blob):
        return Blob(self, container + '/' + blob)


def forbidden(*args, **kwargs):
    raise AssertionError('full hydration/scan/derived rebuild forbidden')


def seed(table, filename, **fields):
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': filename,
                         'processing_complete': True, **fields})


def manifest_key():
    return 'lexical/' + su._lexical_index_manifest_blob_name('u1')


def legacy_key():
    return 'lexical/' + su._lexical_index_json_blob_name('u1')


def refresh(ids=None, **options):
    args = dict(source_version='v2', previous_source_version='v1',
                incremental_only=True, affected_filenames=ids)
    args.update(options)
    return su.refresh_user_lexical_index('u1', **args)


@pytest.fixture
def ctx(monkeypatch):
    table, service = FakeTable(), Service()
    monkeypatch.setitem(su._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(su._CTX, 'blob_service_client', service)
    monkeypatch.setitem(su._CTX, 'blob_lexical_index_container', 'lexical')
    su._clear_manifest_dirty_flag('u1', 'lexical')
    su.invalidate_user_lexical_index_cache('u1')
    seed(table, 'a.jpg', caption='old', photoEmbedding='[1]', semanticEmbedding='[2]')
    seed(table, 'b.jpg', tags='["cat"]')
    snapshot = su._build_user_lexical_index_snapshot('u1', 'v1')
    service.data[legacy_key()] = su._serialize_lexical_index(snapshot)
    service.data[manifest_key()] = json.dumps({
        'userId': 'u1', 'sourceVersion': 'v1', 'schemaVersion': su._LEXICAL_INDEX_SCHEMA_VERSION,
        'rowCount': len(snapshot.rows), 'dirty': False, 'updatedAt': 'v1',
    }).encode()
    yield table, service
    su.invalidate_user_lexical_index_cache('u1')
    su._clear_manifest_dirty_flag('u1', 'lexical')


def test_add_update_delete_matches_full_and_only_point_reads(ctx, monkeypatch):
    table, service = ctx
    table.delete_entity('u1', 'a.jpg')
    seed(table, 'b.jpg', caption='changed', semanticEmbedding='[8]', photoEmbeddingVersion='v9')
    seed(table, '新🙂.jpg', caption='\nquoted "text"', custom='keep', photoEmbedding='[1]')
    expected = su._build_user_lexical_index_snapshot('u1', 'v2')
    reads = []
    original = table.get_entity
    legacy = service.data[legacy_key()]
    with monkeypatch.context() as patch:
        patch.setattr(table, 'get_entity', lambda **kw: (reads.append(kw['row_key']), original(**kw))[1])
        for name in ('_load_lexical_index_blob', '_build_user_lexical_index_snapshot',
                     '_merge_user_lexical_index_snapshot', '_get_dirty_search_index_filenames',
                     '_write_user_listing_index', '_clear_dirty_search_index_filenames'):
            patch.setattr(su, name, forbidden)
        patch.setattr(table, 'query_entities', forbidden)
        import search_db
        patch.setattr(search_db, 'write_for_snapshot', forbidden)
        readall = Download.readall
        patch.setattr(Download, 'readall', lambda self: readall(self) if self.data.startswith(b'{') else forbidden())
        receipt = refresh(['a.jpg', 'b.jpg', '新🙂.jpg', 'b.jpg'])
    assert isinstance(receipt, inc.LexicalPublication)
    assert not hasattr(receipt, 'rows') and receipt.row_count == 2
    assert sorted(reads) == ['a.jpg', 'b.jpg', '新🙂.jpg']
    assert service.data[legacy_key()] == legacy
    payload = json.loads(gzip.decompress(service.data['lexical/' + receipt.blob_name]))
    assert payload == {'userId': 'u1', 'sourceVersion': 'v2', 'schemaVersion': su._LEXICAL_INDEX_SCHEMA_VERSION,
                       'rowCount': 2, 'updatedAt': 'v2', 'rows': expected.rows}
    assert su.get_user_lexical_index('u1', allow_refresh=False)['source_version'] == 'v2'


@pytest.mark.parametrize('fields', [{'processing_complete': False}, {'processing_complete': None},
                                   {'processing_state': ' DeLeTeD '}, {'processing_complete': 1}])
def test_incomplete_and_deleted_are_excluded(ctx, fields):
    seed(ctx[0], 'a.jpg', **fields)
    receipt = refresh(['a.jpg'])
    assert receipt.row_count == 1
    assert [row['RowKey'] for row in su._load_lexical_index_blob('u1').rows] == ['b.jpg']


@pytest.mark.parametrize('failure', ['timeout', 'table_missing', 'download', 'manifest_read'])
def test_transient_read_never_deletes_or_publishes(ctx, monkeypatch, failure):
    table, service = ctx
    before = dict(service.data)
    def fail(*args, **kwargs):
        if failure == 'table_missing':
            error = ResourceNotFoundError('missing table')
            error.error_code = 'TableNotFound'
            raise error
        raise TimeoutError('transient IO')
    if failure in ('timeout', 'table_missing'):
        monkeypatch.setattr(table, 'get_entity', fail)
    elif failure == 'download':
        monkeypatch.setattr(Download, 'readinto', fail)
    else:
        monkeypatch.setattr(Download, 'readall', fail)
    monkeypatch.setattr(su, '_build_user_lexical_index_snapshot', forbidden)
    assert refresh(['a.jpg']) is None
    assert service.data == before


@pytest.mark.parametrize('change', ['missing_manifest', 'missing_blob', 'schema', 'source', 'rowcount',
                                   'header_source', 'header_schema', 'header_count', 'corrupt',
                                   'duplicate_rows', 'truncated', 'trailing', 'pointer', 'dirty_unknown'])
def test_incompatible_base_strictly_defers(ctx, monkeypatch, change):
    _, service = ctx
    manifest = json.loads(service.data[manifest_key()])
    payload = json.loads(gzip.decompress(service.data[legacy_key()]))
    if change == 'missing_manifest':
        service.data.pop(manifest_key())
    elif change == 'missing_blob':
        service.data.pop(legacy_key())
    elif change in ('schema', 'source', 'rowcount', 'pointer', 'dirty_unknown'):
        field, value = {'schema': ('schemaVersion', 'wrong'), 'source': ('sourceVersion', 'old'),
                        'rowcount': ('rowCount', 99), 'pointer': ('blobName', 'other-user.json.gz'),
                        'dirty_unknown': ('dirty', True)}[change]
        manifest[field] = value
        service.data[manifest_key()] = json.dumps(manifest).encode()
    elif change == 'corrupt':
        service.data[legacy_key()] = b'not gzip'
    elif change == 'truncated':
        service.data[legacy_key()] = service.data[legacy_key()][:-5]
    else:
        if change == 'duplicate_rows':
            payload['rows'].append(payload['rows'][0])
        elif change != 'trailing':
            payload[{'header_source': 'sourceVersion', 'header_schema': 'schemaVersion',
                     'header_count': 'rowCount'}[change]] = 'wrong'
        text = json.dumps(payload) + ('x' if change == 'trailing' else '')
        service.data[legacy_key()] = gzip.compress(text.encode())
    before = dict(service.data)
    monkeypatch.setattr(su, '_build_user_lexical_index_snapshot', forbidden)
    assert refresh(['a.jpg'], incremental_only=False) is None
    assert service.data == before


@pytest.mark.parametrize('ids', [None, 'a.jpg', [None], [''], [' a.jpg'], ['x' * 1025]])
def test_explicit_complete_input_is_required(ctx, ids):
    before = dict(ctx[1].data)
    assert refresh(ids) is None
    assert ctx[1].data == before


@pytest.mark.parametrize('options', [{'previous_source_version': None}, {'previous_source_version': 'wrong'},
                                     {'source_version': 'v1'}, {'force_full': True}])
def test_source_and_force_full_validation(ctx, options):
    before = dict(ctx[1].data)
    assert refresh(['a.jpg'], **options) is None
    assert ctx[1].data == before


def test_empty_complete_set_is_valid_version_only_merge(ctx):
    assert refresh([]).row_count == 2


def test_bounded_input_stops_generator(ctx, monkeypatch):
    monkeypatch.setattr(inc, 'MAX_IDS', 2)
    seen = []
    def ids():
        for i in range(1000):
            seen.append(i)
            yield str(i)
    assert refresh(ids()) is None
    assert seen == [0, 1, 2]


@pytest.mark.parametrize('flag', ['has_more', 'truncated'])
def test_truncated_explicit_set_is_rejected_before_iteration(ctx, flag):
    class Incomplete:
        def __iter__(self):
            raise AssertionError('known incomplete capture must not be iterated')
    setattr(Incomplete, flag, True)
    before = dict(ctx[1].data)
    assert refresh(Incomplete()) is None
    assert ctx[1].data == before


@pytest.mark.parametrize('budget', ['row', 'dirty', 'manifest'])
def test_memory_budgets_defer(ctx, monkeypatch, budget):
    if budget == 'manifest':
        monkeypatch.setattr(inc, 'MAX_MANIFEST_BYTES', 5)
    else:
        seed(ctx[0], 'a.jpg', caption='x' * 1000)
        monkeypatch.setattr(inc, 'MAX_ITEM_CHARS' if budget == 'row' else 'MAX_DIRTY_BYTES', 100)
    before = dict(ctx[1].data)
    assert refresh(['a.jpg']) is None
    assert ctx[1].data == before


@pytest.mark.parametrize('failure', ['data', 'manifest', 'cas', 'lease'])
def test_publication_failures_keep_committed_blob(ctx, monkeypatch, failure):
    _, service = ctx
    before = service.data[manifest_key()]
    old_blob = service.data[legacy_key()]
    original = Blob.upload_blob
    def upload(self, data, **options):
        if options.get('overwrite') is False:
            if failure == 'data':
                raise IOError('failed data upload')
            if failure == 'cas':
                concurrent = json.loads(before)
                concurrent['concurrentField'] = 'concurrent'
                service.data[manifest_key()] = json.dumps(concurrent).encode()
        elif self.name == manifest_key() and failure == 'manifest':
            raise IOError('failed manifest upload')
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    if failure == 'lease':
        import index_publication
        monkeypatch.setattr(index_publication, 'check', lambda: (_ for _ in ()).throw(RuntimeError('lost lease')))
    monkeypatch.setattr(su, '_build_user_lexical_index_snapshot', forbidden)
    assert refresh(['a.jpg']) is None
    assert service.data[legacy_key()] == old_blob
    if failure != 'cas':
        assert service.data[manifest_key()] == before
    assert su._load_lexical_index_blob('u1').source_version == 'v1'


def test_streamed_pointer_base_second_merge_and_dirty_provenance(ctx):
    first = refresh(['a.jpg'])
    manifest = json.loads(ctx[1].data[manifest_key()])
    manifest.update(dirty=True, baseSourceVersion='v2', sourceVersion='v3')
    ctx[1].data[manifest_key()] = json.dumps(manifest).encode()
    seed(ctx[0], 'b.jpg', caption='latest')
    second = refresh(['b.jpg'], previous_source_version='v2', source_version='v3')
    assert second.blob_name != first.blob_name
    assert su._load_lexical_index_blob('u1').source_version == 'v3'
    committed = su._load_lexical_index_manifest('u1')
    assert committed['dirty'] is True
    assert committed['sourceVersion'] == 'v3'
    assert committed['lexicalSnapshotSourceVersion'] == 'v3'
    assert committed['lexicalSnapshotDirty'] is False


def test_snapshot_target_is_independent_of_shared_dirty_token(ctx):
    manifest = json.loads(ctx[1].data[manifest_key()])
    manifest.update(dirty=True, baseSourceVersion='v1', sourceVersion='v3')
    ctx[1].data[manifest_key()] = json.dumps(manifest).encode()
    assert refresh(['a.jpg'], source_version='v2') is not None
    committed = su._load_lexical_index_manifest('u1')
    assert committed['sourceVersion'] == 'v3'
    assert committed['baseSourceVersion'] == 'v1'
    assert committed['dirty'] is True
    assert committed['lexicalSnapshotSourceVersion'] == 'v2'


def test_losing_cas_cannot_overwrite_winners_immutable_data(ctx, monkeypatch):
    _, service = ctx
    old_fixed = service.data[legacy_key()]
    original = Blob.upload_blob
    winning_name = su._lexical_index_json_blob_name('u1')[:-len('.json.gz')] + '.winner.json.gz'
    payload = json.loads(gzip.decompress(old_fixed))
    payload.update(sourceVersion='v3', updatedAt='v3')
    payload['rows'][0]['caption'] = 'winner'
    winner = gzip.compress(json.dumps(payload).encode())
    def upload(self, data, **options):
        if options.get('overwrite') is False:
            service.data['lexical/' + winning_name] = winner
            manifest = json.loads(service.data[manifest_key()])
            manifest.update(sourceVersion='v3', updatedAt='v3', blobName=winning_name)
            service.data[manifest_key()] = json.dumps(manifest).encode()
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    assert refresh(['a.jpg']) is None
    assert service.data['lexical/' + winning_name] == winner
    assert service.data[legacy_key()] == old_fixed
    loaded = su._load_lexical_index_blob('u1')
    assert loaded.source_version == 'v3' and loaded.rows[0]['caption'] == 'winner'


def test_loader_missing_pointer_does_not_fall_back(ctx):
    receipt = refresh(['a.jpg'])
    ctx[1].data.pop('lexical/' + receipt.blob_name)
    su.invalidate_user_lexical_index_cache('u1')
    assert su._load_lexical_index_blob('u1') is None


def test_default_snapshot_api_and_full_builder_are_preserved(ctx, monkeypatch):
    import search_db
    monkeypatch.setattr(search_db, 'write_for_snapshot', lambda *args: True)
    snapshot = su.refresh_user_lexical_index('u1', source_version='full', force_full=True)
    assert isinstance(snapshot, su.LexicalIndexSnapshot)
    assert snapshot.source_version == 'full'
    assert len(snapshot.rows) == 2
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['blobName'].endswith('.json.gz')
    assert manifest['sourceVersion'] == manifest['lexicalSnapshotSourceVersion'] == 'full'
    assert manifest['dirty'] is manifest['lexicalSnapshotDirty'] is False


def test_isolated_upgrade_preserves_shared_listing_and_sqlite_lineage(ctx, monkeypatch):
    import search_db
    _, service = ctx
    snapshot = su._load_lexical_index_blob('u1')
    su._write_user_listing_index('u1', snapshot)
    search_manifest = {'sourceVersion': 'v1', 'schemaVersion': search_db.SCHEMA_VERSION}
    monkeypatch.setattr(search_db, 'load_manifest', lambda user: dict(search_manifest))
    shared_before = su._load_lexical_index_manifest('u1')
    listing_before = {key: value for key, value in service.data.items() if 'listing' in key}
    receipt = refresh(['a.jpg'])
    committed = su._load_lexical_index_manifest('u1')
    assert all(committed[key] == value for key, value in shared_before.items())
    assert {key: value for key, value in service.data.items() if 'listing' in key} == listing_before
    assert search_db.is_current('u1', committed['sourceVersion'])
    assert not search_db.is_current('u1', receipt.source_version)
    assert su.get_user_listing_index('u1', allow_refresh=False)['source_version'] == 'v1'
    assert su.get_user_lexical_index('u1', allow_refresh=False)['source_version'] == 'v2'


def test_real_sqlite_full_build_then_isolated_upgrade_keeps_durable_lineage(ctx, monkeypatch):
    import search_db
    _, service = ctx
    monkeypatch.setattr(search_db, '_blob_client', lambda name: service.get_blob_client('search', name))
    search_db.invalidate_manifest_cache('u1')
    try:
        snapshot = su.refresh_user_lexical_index('u1', source_version='v1', force_full=True)
        assert isinstance(snapshot, su.LexicalIndexSnapshot)
        assert search_db.is_current('u1', 'v1')
        search_before = {key: value for key, value in service.data.items() if key.startswith('search/')}
        listing_before = {key: value for key, value in service.data.items() if 'listing' in key}
        seed(ctx[0], 'a.jpg', caption='snapshot only')
        assert refresh(['a.jpg'])
        assert {key: value for key, value in service.data.items() if key.startswith('search/')} == search_before
        assert {key: value for key, value in service.data.items() if 'listing' in key} == listing_before
        assert search_db.is_current('u1', su._load_lexical_index_manifest('u1')['sourceVersion'])
        assert not search_db.is_current('u1', 'v2')
        assert su.get_user_lexical_index('u1', allow_refresh=False)['source_version'] == 'v2'
    finally:
        search_db.invalidate_manifest_cache('u1')


def test_repeated_real_touch_preserves_snapshot_base_and_shared_dirty_latch(ctx):
    first_touch = su.touch_user_lexical_index_state('u1')
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['baseSourceVersion'] == manifest['lexicalSnapshotSourceVersion'] == 'v1'
    assert manifest['rowCount'] == 2
    first = refresh(['a.jpg'], source_version=first_touch)
    assert first is not None
    assert su._INDEX_MANIFEST_DIRTY_FLAGS[('u1', 'lexical')]
    assert su._load_lexical_index_manifest('u1')['dirty'] is True
    assert su.get_user_lexical_index('u1', allow_refresh=False)['source_version'] == first_touch
    second_touch = su.touch_user_lexical_index_state('u1')
    third_touch = su.touch_user_lexical_index_state('u1')
    touched = su._load_lexical_index_manifest('u1')
    assert second_touch and third_touch
    assert touched['baseSourceVersion'] == 'v1'
    assert touched['blobName'] == first.blob_name
    assert touched['lexicalSnapshotSourceVersion'] == first_touch
    assert touched['lexicalSnapshotDirty'] is True
    assert su.get_user_lexical_index('u1', allow_refresh=False) is None
    seed(ctx[0], 'b.jpg', caption='after repeated touches')
    second = refresh(['b.jpg'], source_version=third_touch, previous_source_version=first_touch)
    assert second is not None and second.blob_name != first.blob_name
    committed = su._load_lexical_index_manifest('u1')
    assert committed['baseSourceVersion'] == 'v1' and committed['dirty'] is True
    assert committed['sourceVersion'] == third_touch
    assert committed['lexicalSnapshotSourceVersion'] == second.source_version
    assert committed['lexicalSnapshotDirty'] is False
    assert su._INDEX_MANIFEST_DIRTY_FLAGS[('u1', 'lexical')]


@pytest.mark.parametrize('failure', ['read', 'upload', 'cas', 'unconfigured'])
def test_touch_failure_rearms_retry_without_losing_provenance(ctx, monkeypatch, failure):
    _, service = ctx
    first = refresh(['a.jpg'])
    before = service.data[manifest_key()]
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise (ResourceModifiedError('lost CAS') if failure == 'cas' else IOError('storage failed'))
        if failure == 'read':
            patch.setattr(Download, 'readall', fail)
        elif failure in ('upload', 'cas'):
            patch.setattr(Blob, 'upload_blob', fail)
        else:
            patch.setattr(su, '_lexical_index_blob_client', lambda name: None)
        assert su.touch_user_lexical_index_state('u1')
    assert service.data[manifest_key()] == before
    assert not su._INDEX_MANIFEST_DIRTY_FLAGS.get(('u1', 'lexical'))
    assert su.touch_user_lexical_index_state('u1')
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['blobName'] == first.blob_name
    assert manifest['lexicalSnapshotSourceVersion'] == 'v2'
    assert manifest['baseSourceVersion'] == 'v1'
    assert manifest['dirty'] is manifest['lexicalSnapshotDirty'] is True


@pytest.mark.parametrize('field,value', [('lexicalSnapshotSourceVersion', 'wrong'),
                                        ('lexicalSnapshotSourceVersion', ''),
                                        ('lexicalSnapshotRowCount', 99),
                                        ('schemaVersion', 'wrong'),
                                        ('userId', 'other')])
def test_loader_validates_authoritative_snapshot_provenance(ctx, field, value):
    refresh(['a.jpg'])
    manifest = su._load_lexical_index_manifest('u1')
    manifest[field] = value
    ctx[1].data[manifest_key()] = json.dumps(manifest).encode()
    su.invalidate_user_lexical_index_cache('u1')
    assert su._load_lexical_index_blob('u1') is None
    assert su.get_user_lexical_index('u1', allow_refresh=False) is None


@pytest.mark.parametrize('pointer', [None, '', 123, 'u1.lexical.other.json.gz'])
def test_loader_present_invalid_pointer_never_uses_legacy(ctx, pointer):
    manifest = su._load_lexical_index_manifest('u1')
    manifest['blobName'] = pointer
    ctx[1].data[manifest_key()] = json.dumps(manifest).encode()
    assert su._load_lexical_index_blob('u1') is None
    with pytest.raises(ValueError, match='pointer'):
        su.get_lexical_index_blob_location('u1')


def test_snapshot_blob_location_uses_committed_pointer_for_sas_consumers(ctx, monkeypatch):
    import search_db
    assert su.get_lexical_index_blob_location('u1') == ('lexical', su._lexical_index_json_blob_name('u1'))
    receipt = refresh(['a.jpg'])
    assert su.get_lexical_index_blob_location('u1') == ('lexical', receipt.blob_name)
    monkeypatch.setattr(search_db, 'write_for_snapshot', lambda *args: True)
    assert su.refresh_user_lexical_index('u1', source_version='full', force_full=True)
    manifest = su._load_lexical_index_manifest('u1')
    assert su.get_lexical_index_blob_location('u1') == ('lexical', manifest['blobName'])


@pytest.mark.parametrize('failure', ['data', 'manifest', 'cas', 'properties', 'listing',
                                     'listing_manifest', 'search', 'storage'])
def test_default_publication_failure_retains_dirty_and_never_caches_or_acks(ctx, monkeypatch, failure):
    import search_db
    _, service = ctx
    su.touch_user_lexical_index_state('u1')
    before = service.data[manifest_key()]
    legacy = service.data[legacy_key()]
    monkeypatch.setattr(search_db, 'write_for_snapshot', lambda *args: failure != 'search')
    monkeypatch.setattr(su, '_clear_dirty_search_index_filenames', forbidden)
    original = Blob.upload_blob
    def upload(self, data, **options):
        if failure == 'data' and options.get('overwrite') is False:
            raise IOError('data upload failed')
        if failure == 'manifest' and self.name == manifest_key():
            raise IOError('manifest upload failed')
        if failure == 'listing' and 'listing' in self.name and self.name.endswith('.json.gz'):
            raise IOError('listing upload failed')
        if failure == 'listing_manifest' and 'listing' in self.name and self.name.endswith('.json'):
            raise IOError('listing manifest upload failed')
        if failure == 'cas' and options.get('overwrite') is False:
            concurrent = json.loads(before)
            concurrent['concurrentField'] = True
            service.data[manifest_key()] = json.dumps(concurrent).encode()
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    if failure == 'properties':
        monkeypatch.setattr(Blob, 'get_blob_properties', lambda self: (_ for _ in ()).throw(IOError('read failed')))
    if failure == 'storage':
        monkeypatch.setattr(su, '_lexical_index_blob_client', lambda name: None)
    assert su.refresh_user_lexical_index('u1', source_version='full', force_full=True) is None
    assert 'u1' not in su._LEXICAL_INDEX_CACHE
    assert service.data[legacy_key()] == legacy
    committed = json.loads(service.data[manifest_key()])
    assert committed['dirty'] is committed['lexicalSnapshotDirty'] is True
    assert committed['lexicalSnapshotSourceVersion'] == 'v1'
    if failure != 'cas':
        assert service.data[manifest_key()] == before


def test_default_manifest_error_propagates_original_under_publication_guard(ctx, monkeypatch):
    import index_publication
    import search_db
    monkeypatch.setattr(search_db, 'write_for_snapshot', lambda *args: True)
    error = IOError('manifest write failed')
    original = Blob.upload_blob
    def upload(self, data, **options):
        if self.name == manifest_key():
            raise error
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    guard = SimpleNamespace(check=lambda: None)
    with pytest.raises(IOError) as raised, index_publication.guarded_publication(guard):
        su.refresh_user_lexical_index('u1', source_version='full', force_full=True)
    assert raised.value is error
    assert su._load_lexical_index_manifest('u1')['sourceVersion'] == 'v1'


def test_failed_isolated_upgrade_keeps_shared_and_snapshot_dirty(ctx, monkeypatch):
    su.touch_user_lexical_index_state('u1')
    before = ctx[1].data[manifest_key()]
    original = Blob.upload_blob
    def upload(self, data, **options):
        if self.name == manifest_key():
            raise IOError('manifest failed')
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    assert refresh(['a.jpg']) is None
    assert ctx[1].data[manifest_key()] == before
    assert su._INDEX_MANIFEST_DIRTY_FLAGS[('u1', 'lexical')]


def test_full_refresh_resets_independent_snapshot_only_after_dependent_coverage(ctx, monkeypatch):
    import search_db
    su.touch_user_lexical_index_state('u1')
    assert refresh(['a.jpg'])
    calls = []
    def write(user, snapshot):
        # Shared commit/cache/dirty ack has not happened yet.
        assert su._load_lexical_index_manifest(user)['lexicalSnapshotSourceVersion'] == 'v2'
        calls.append(snapshot.source_version)
        return True
    monkeypatch.setattr(search_db, 'write_for_snapshot', write)
    snapshot = su.refresh_user_lexical_index('u1', source_version='full', force_full=True)
    assert isinstance(snapshot, su.LexicalIndexSnapshot)
    assert calls == ['full']
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['sourceVersion'] == manifest['lexicalSnapshotSourceVersion'] == 'full'
    assert manifest['dirty'] is manifest['lexicalSnapshotDirty'] is False
    assert su.get_user_lexical_index('u1', allow_refresh=False)['source_version'] == 'full'


def test_snapshot_row_count_does_not_relabel_shared_catalog_count(ctx):
    ctx[0].delete_entity('u1', 'a.jpg')
    assert refresh(['a.jpg']).row_count == 1
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['rowCount'] == 2 and manifest['lexicalSnapshotRowCount'] == 1
    assert su.get_user_lexical_index('u1', allow_refresh=False)['rows'][0]['RowKey'] == 'b.jpg'
    su.touch_user_lexical_index_state('u1')
    assert refresh([], previous_source_version='v2', source_version='v3').row_count == 1


@pytest.mark.parametrize('explicit', [False, True])
def test_real_touch_during_publication_fences_cas_and_keeps_snapshot_base(ctx, monkeypatch, explicit):
    import search_db
    monkeypatch.setattr(search_db, 'write_for_snapshot', lambda *args: True)
    original = Blob.upload_blob
    touches = []
    def upload(self, data, **options):
        if self.name.endswith('.json.gz') and options.get('overwrite') is False:
            touches.append(su.touch_user_lexical_index_state('u1'))
        return original(self, data, **options)
    monkeypatch.setattr(Blob, 'upload_blob', upload)
    monkeypatch.setattr(su, '_clear_dirty_search_index_filenames', forbidden)
    result = (refresh(['a.jpg']) if explicit else
              su.refresh_user_lexical_index('u1', source_version='full', force_full=True))
    assert result is None and len(touches) == 1
    manifest = su._load_lexical_index_manifest('u1')
    assert manifest['sourceVersion'] == touches[0]
    assert manifest['dirty'] is manifest['lexicalSnapshotDirty'] is True
    assert manifest['lexicalSnapshotSourceVersion'] == manifest['baseSourceVersion'] == 'v1'
    assert su._load_lexical_index_blob('u1').source_version == 'v1'
    assert 'u1' not in su._LEXICAL_INDEX_CACHE


@pytest.mark.parametrize('streamed', [False, True])
def test_parser_and_merge_ram_does_not_scale_with_base(tmp_path, streamed):
    import sqlite3
    def peak(count):
        base, spool = tmp_path / 'base.gz', tmp_path / 'spool.gz'
        header = {'userId': 'u1', 'sourceVersion': 'v1', 'schemaVersion': 'v1',
                  'rowCount': count, 'updatedAt': 'v1'}
        if streamed:
            with RowsWriter(base, header) as writer:
                for index in range(count):
                    writer.add({'RowKey': str(index), 'caption': 'x' * 256})
        else:
            # Write the old single-line format without allocating the fixture.
            with gzip.open(base, 'wt') as stream:
                stream.write(json.dumps(header)[:-1] + ',"rows":[')
                for index in range(count):
                    if index:
                        stream.write(',')
                    stream.write(json.dumps({'RowKey': str(index), 'caption': 'x' * 256}))
                stream.write(']}')
        db = sqlite3.connect(str(tmp_path / ('keys' + str(count) + '.sqlite')))
        db.execute('PRAGMA cache_size = -2048')
        db.execute('CREATE TABLE keys (filename TEXT PRIMARY KEY) WITHOUT ROWID')
        tracemalloc.start()
        try:
            assert inc._merge_file(base, spool, db, {'0': None}, header, 'u1', 'v1', 'v1') == count - 1
            _, result = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            db.close()
        assert sum(1 for _ in iter_rows(spool)) == count - 1
        return result
    small, large = peak(200), peak(20000)
    assert large < small + 2 * 1024 * 1024
    assert large < 4 * 1024 * 1024


def test_parallel_read_failure_preserves_manifest_and_original_guard(ctx, monkeypatch):
    import threading
    import index_publication
    table, service = ctx
    before = dict(service.data)
    original = table.get_entity
    barrier = threading.Barrier(2, timeout=3)
    guard = object()
    monkeypatch.setenv('INDEX_SOURCE_READ_CONCURRENCY', '2')

    def read(**kwargs):
        # Actual integration must propagate publication context to the SDK read.
        assert index_publication._ACTIVE.get() is guard
        barrier.wait()
        if kwargs['row_key'] == 'b.jpg':
            raise TimeoutError('second source read failed')
        return original(**kwargs)

    monkeypatch.setattr(table, 'get_entity', read)
    token = index_publication._ACTIVE.set(guard)
    try:
        assert refresh(['a.jpg', 'b.jpg']) is None
    finally:
        index_publication._ACTIVE.reset(token)
    assert service.data == before
