import copy
import json
from unittest.mock import Mock

import pytest
from azure.core.exceptions import AzureError, ResourceNotFoundError

import app
from upload_waves import Conflict, ShadowWaves
from upload_wave_shadow import STEPS, compare_against_legacy, observe_source, reconcile_page, resolve_source, source_identity
from tests.test_upload_waves import AtomicTable, close, finish_scan


@pytest.fixture
def source_world():
    table = AtomicTable()
    store = ShadowWaves(table, 'library-A')
    row = {'PartitionKey': 'library-A', 'RowKey': 'original.jpg', 'anonymousImageId': 'physical-1',
           'fileHash': 'hash-1', 'faceCount': 0, **{step + '_status': 'done' for step in STEPS}}
    table.submit_transaction([('create', row)])
    session = store.open_session('parent', 1)
    store.register('parent', [{'itemId': 'i', 'filename': 'selected.jpg'}])
    resolved = resolve_source(store, table, 'parent', 'i', 'accepted', 'original.jpg')
    close(store, 'parent', resolved)
    wave = session['waveId']
    member = store._page(f'e!{wave}!m!')[0]
    return store, table, wave, member


def test_reconcile_then_evaluate_uses_persisted_zero_face_proof(source_world):
    store, table, wave, _ = source_world
    lookup = Mock(return_value=[])
    result = reconcile_page(store, wave, table, table, lookup)
    assert result == dict(scanned=1, nextCursor='', evidenceQualified=False, dispatchable=False)
    lookup.assert_called_once_with('library-A', 'original.jpg')
    assert finish_scan(store, wave)['state'] == 'ready'


def test_source_identity_is_unqualified_without_a_real_processing_revision():
    row = {'anonymousImageId': 'physical-1', 'fileHash': 'hash-1'}
    photo, revision, qualified = source_identity(row)
    assert photo == 'physical-1' and qualified is False
    # Same proxy digest every time for the same (photo, hash) -- legacy rows
    # must still produce a stable, comparable revision.
    assert source_identity(dict(row)) == (photo, revision, False)


def test_source_identity_is_qualified_once_a_real_revision_is_stamped():
    row = {'anonymousImageId': 'physical-1', 'fileHash': 'hash-1', 'processingRevision': 'gen-abc'}
    photo, revision, qualified = source_identity(row)
    assert (photo, revision, qualified) == ('physical-1', 'gen-abc', True)


def test_reconcile_reports_qualified_evidence_once_rows_carry_a_real_revision(source_world):
    store, table, wave, member = source_world
    table.rows['library-A', 'original.jpg']['processingRevision'] = member['processingRevision']
    lookup = Mock(return_value=[])
    result = reconcile_page(store, wave, table, table, lookup)
    assert result['evidenceQualified'] is True
    observed = store._page(f'e!{wave}!m!')[0]
    assert observed['evidenceQualified'] is True


def test_reconcile_mixed_qualification_across_a_page_reports_unqualified(source_world):
    # Page-scoped evidenceQualified is an AND across every member scanned on
    # that page -- one legacy (proxy-revision) row among otherwise-qualified
    # rows must not be reported as if every member were qualified.
    store, table, wave, member = source_world
    table.submit_transaction([('create', {
        'PartitionKey': 'library-A', 'RowKey': 'second.jpg', 'anonymousImageId': 'physical-2',
        'fileHash': 'hash-2', 'faceCount': 0, 'processingRevision': 'gen-xyz',
        **{step + '_status': 'done' for step in STEPS},
    })])
    second_revision = source_identity(table.rows['library-A', 'second.jpg'])[1]
    table.submit_transaction([('create', {
        'PartitionKey': 'library-A', 'RowKey': f"e!{wave}!m!second",
        'kind': 'member', 'waveId': wave, 'photoId': 'physical-2',
        'processingRevision': second_revision, 'filename': 'second.jpg',
        'requiredSteps': member['requiredSteps'], 'outcome': 'pending', 'evidenceId': '',
        'outstandingLeases': 0, 'evidenceQualified': False, 'observedAt': 0.0,
    })])
    result = reconcile_page(store, wave, table, table, lambda *a: [])
    assert result['evidenceQualified'] is False


@pytest.mark.parametrize('step', STEPS)
@pytest.mark.parametrize('status', ['pending', 'running', 'failed', '', 'retryable'])
def test_each_unfinished_step_blocks_readiness(source_world, step, status):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg'][step + '_status'] = status
    observation = observe_source(store, member, table, table, lambda *a: [])
    assert observation['outcome'] == 'pending'


@pytest.mark.parametrize('field,value', [('processing_lease_owner', 'old-worker'),
    ('processing_lease', 'old-browser'), ('processing_lease_expires_at', '2000-01-01T00:00:00Z')])
def test_expired_or_uncleared_lease_still_blocks(source_world, field, value):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg'][field] = value
    result = observe_source(store, member, table, table, lambda *a: [])
    assert result['outcome'] == 'pending' and result['outstanding_leases'] == 1


@pytest.mark.parametrize('ids,count', [(None, 0), ([], 1), (['f', 'f'], 2), (['f'], True)])
def test_incomplete_or_inconsistent_lookup_blocks(source_world, ids, count):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg']['faceCount'] = count
    assert observe_source(store, member, table, table, lambda *a: ids)['outcome'] == 'pending'


@pytest.mark.parametrize('patch', [dict(filename='other.jpg'), dict(embedding='[]'),
                                  dict(embedding='[0,0]'), dict(embedding='[true,1]'),
                                  dict(embedding='[NaN,1]'), dict(embeddingVersion='')])
def test_face_rows_require_identity_and_valid_embedding(source_world, patch):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg']['faceCount'] = 1
    face = dict(PartitionKey='library-A', RowKey='f', filename='original.jpg',
                embedding='[1,0]', embeddingVersion='face-v1')
    face.update(patch)
    table.submit_transaction([('create', face)])
    assert observe_source(store, member, table, table, lambda *a: ['f'])['outcome'] == 'pending'


def test_durable_face_result_settles_but_missing_face_waits(source_world):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg']['faceCount'] = 1
    assert observe_source(store, member, table, table, lambda *a: ['f'])['outcome'] == 'pending'
    table.submit_transaction([('create', dict(PartitionKey='library-A', RowKey='f',
        filename='original.jpg', embedding='[1,0]', embeddingVersion='face-v1'))])
    assert observe_source(store, member, table, table, lambda *a: ['f'])['outcome'] == 'settled'


def test_source_replacement_and_missing_source_are_not_completion(source_world):
    store, table, _, member = source_world
    table.rows['library-A', 'original.jpg']['anonymousImageId'] = 'replacement'
    assert observe_source(store, member, table, table, lambda *a: [])['outcome'] == 'pending'
    del table.rows['library-A', 'original.jpg']
    assert observe_source(store, member, table, table, lambda *a: [])['outcome'] == 'pending'


def test_transport_failure_does_not_rewrite_completion(source_world):
    store, table, wave, _ = source_world
    prior = copy.deepcopy(table.rows)
    bad = Mock()
    bad.get_entity.side_effect = AzureError('storage unavailable')
    with pytest.raises(AzureError):
        reconcile_page(store, wave, bad, table, lambda *a: [])
    assert prior == table.rows


def test_reconciliation_is_bounded_and_rejects_foreign_cursor(source_world):
    store, table, wave, _ = source_world
    with pytest.raises(ValueError, match='cursor'):
        reconcile_page(store, wave, table, table, lambda *a: [], after="bad' or true")
    reconcile_page(store, wave, table, table, lambda *a: [])
    assert table.queries[-1][1] == 20
    assert f'e!{wave}!m!' in table.queries[-1][0]


@pytest.fixture
def api(monkeypatch):
    table = AtomicTable()
    library = ['library-A']
    monkeypatch.setenv('UPLOAD_WAVE_SHADOW_ENABLED', 'true')
    monkeypatch.setattr(app, 'jobs_table_client', table)
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'face_table_client', table)
    monkeypatch.setattr(app, 'get_face_ids_for_filename', lambda *a: [])
    monkeypatch.setattr(app, '_require_library_context', lambda **kw: ('account', library[0], None))
    # Every route must be incapable of releasing current or future workers.
    for name in ('enqueue_index_build', '_enqueue_clustering_job', 'enqueue_ipwork_release'):
        monkeypatch.setattr(app, name, Mock(side_effect=AssertionError('shadow released real work')))
    return app.app.test_client(), table, library


ROOT = '/upload/waves/shadow'


def test_api_disabled_by_default_before_any_storage_read(api, monkeypatch):
    client, table, _ = api
    monkeypatch.delenv('UPLOAD_WAVE_SHADOW_ENABLED')
    response = client.post(ROOT + '/sessions', json={'sessionId': 'p', 'declaredCount': 0})
    assert response.status_code == 404 and not table.rows


def test_api_requires_authenticated_library(api, monkeypatch):
    client, table, _ = api
    monkeypatch.setattr(app, '_require_library_context',
                        lambda **kw: (None, None, (app.jsonify({'error': 'unauthorized'}), 401)))
    assert client.post(ROOT + '/sessions', json={}).status_code == 401
    assert not table.rows


def test_full_api_flow_and_library_isolation(api):
    client, table, library = api
    opened = client.post(ROOT + '/sessions', json={'sessionId': 'parent', 'declaredCount': 1})
    assert opened.status_code == 200
    wave = opened.json['waveId']
    assert opened.json['protocolMode'] == 'shadow' and not opened.json['dispatchable']
    assert opened.json['evidenceQualified'] is False
    assert client.post(ROOT + '/sessions/parent/items', json={
        'items': [{'itemId': 'i', 'filename': 'selected.jpg'}]}).status_code == 200
    table.submit_transaction([('create', dict(PartitionKey=library[0], RowKey='actual.jpg',
        anonymousImageId='physical-1', fileHash='hash-1', faceCount=0,
        **{step + '_status': 'done' for step in STEPS}))])
    resolved = client.post(ROOT + '/sessions/parent/resolve', json={
        'itemId': 'i', 'outcome': 'accepted', 'filename': 'actual.jpg'})
    assert resolved.status_code == 200
    closed = client.post(ROOT + '/sessions/parent/close', json={
        'manifestRevision': resolved.json['manifestRevision'], 'closeId': 'close-1'})
    assert closed.status_code == 200
    assert client.post(ROOT + '/' + wave + '/evaluate', json={}).json['state'] == 'processing'
    assert client.post(ROOT + '/' + wave + '/reconcile', json={'outcome': 'settled'}).status_code == 200
    assert client.post(ROOT + '/' + wave + '/evaluate', json={}).json['state'] == 'ready'
    status = client.get(ROOT + '/' + wave)
    assert status.status_code == 200 and status.headers['Cache-Control'] == 'no-store'
    assert 'evaluation' not in status.json and 'PartitionKey' not in status.json
    library[0] = 'library-B'
    assert client.get(ROOT + '/' + wave).status_code == 409
    assert client.post(ROOT + '/sessions/parent/resolve', json={
        'itemId': 'i', 'outcome': 'accepted', 'filename': 'actual.jpg'}).status_code == 409


def test_client_complete_flag_does_not_settle_pending_source(api):
    client, table, library = api
    wave = client.post(ROOT + '/sessions', json={'sessionId': 'p', 'declaredCount': 1}).json['waveId']
    client.post(ROOT + '/sessions/p/items', json={'items': [{'itemId': 'i', 'filename': 'a.jpg'}]})
    table.submit_transaction([('create', dict(PartitionKey=library[0], RowKey='a.jpg',
        anonymousImageId='physical', fileHash='hash', faceCount=0, face_status='running'))])
    row = client.post(ROOT + '/sessions/p/resolve', json={'itemId': 'i', 'outcome': 'accepted', 'filename': 'a.jpg'}).json
    client.post(ROOT + '/sessions/p/close', json={'manifestRevision': row['manifestRevision'], 'closeId': 'close'})
    client.post(ROOT + '/' + wave + '/reconcile', json={'complete': True, 'outcome': 'settled'})
    assert client.post(ROOT + '/' + wave + '/evaluate', json={'complete': True}).json['state'] == 'processing'


def test_compare_against_legacy_reports_counts_and_barrier_wait(source_world):
    store, table, wave, _ = source_world
    store.clock = lambda: 1000.0
    reconcile_page(store, wave, table, table, lambda *a: [])
    finish_scan(store, wave)
    result = compare_against_legacy(store, wave)
    assert result['waveId'] == wave and result['state'] == 'ready'
    assert result['memberCount'] == 1 and result['settledMemberCount'] == 1
    # source_world's row has no real processingRevision, so source_identity
    # falls back to the legacy hash proxy -- unqualified evidence.
    assert result['qualifiedMemberCount'] == 0
    assert result['barrierAt'] == 1000.0
    assert result['extraWaitSecondsMin'] == result['extraWaitSecondsMax'] == 0.0


def test_compare_against_legacy_reports_qualified_members_and_nonzero_wait(source_world):
    store, table, wave, member = source_world
    table.rows['library-A', 'original.jpg']['processingRevision'] = member['processingRevision']
    observed_at = [100.0]
    store.clock = lambda: observed_at[0]
    reconcile_page(store, wave, table, table, lambda *a: [])
    observed_at[0] = 142.0
    finish_scan(store, wave)
    result = compare_against_legacy(store, wave)
    assert result['qualifiedMemberCount'] == 1
    assert result['barrierAt'] == 142.0
    assert result['extraWaitSecondsMin'] == result['extraWaitSecondsMax'] == 42.0


def test_compare_against_legacy_rejects_unknown_or_foreign_wave(source_world):
    store, table, wave, _ = source_world
    with pytest.raises(Conflict):
        compare_against_legacy(store, 'no-such-wave')
    with pytest.raises(Conflict):
        compare_against_legacy(ShadowWaves(table, 'library-B'), wave)


@pytest.fixture
def tools_api(monkeypatch):
    table = AtomicTable()
    library = ['library-A']
    monkeypatch.setenv('UPLOAD_WAVE_SHADOW_ENABLED', 'true')
    monkeypatch.setattr(app, 'jobs_table_client', table)
    monkeypatch.setattr(app, '_require_library_context', lambda **kw: ('account', library[0], None))
    return app.app.test_client(), table, library


def test_compare_route_disabled_returns_404(tools_api, monkeypatch):
    client, _, _ = tools_api
    monkeypatch.delenv('UPLOAD_WAVE_SHADOW_ENABLED')
    assert client.get('/api/tools/waves/shadow/some-wave/compare').status_code == 404


def test_compare_route_returns_diagnostic_for_a_ready_wave(tools_api):
    client, table, _ = tools_api
    store = ShadowWaves(table, 'library-A')
    row = store.open_session('p', 1)
    store.register('p', [{'itemId': 'i', 'filename': 'a.jpg'}])
    resolved = store.resolve('p', 'i', outcome='accepted', photo_id='photo-1',
                             processing_revision='rev-1', filename='a.jpg', required_steps=['face'])
    close(store, 'p', resolved)
    wave = row['waveId']
    store.observe(wave, 'photo-1', 'rev-1', outcome='settled', evidence_id='e')
    finish_scan(store, wave)
    response = client.get(f'/api/tools/waves/shadow/{wave}/compare')
    assert response.status_code == 200
    assert response.json['waveId'] == wave and response.json['state'] == 'ready'
    assert response.headers['Cache-Control'] == 'no-store'


def test_compare_route_cannot_read_another_librarys_wave(tools_api):
    client, table, library = tools_api
    store = ShadowWaves(table, 'library-A')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    wave = row['waveId']
    library[0] = 'library-B'
    assert client.get(f'/api/tools/waves/shadow/{wave}/compare').status_code == 404


@pytest.mark.parametrize('body', [None, [], {'sessionId': 'bad/filter', 'declaredCount': 0},
                                  {'sessionId': 'p', 'declaredCount': True}])
def test_invalid_api_request_has_no_writes(api, body):
    client, table, _ = api
    response = client.post(ROOT + '/sessions', json=body)
    assert response.status_code == 400 and not table.rows


def test_storage_errors_are_retryable_and_no_partial_admission_is_committed(api):
    client, table, _ = api
    def unavailable():
        raise AzureError('connection lost')
    table.before_commit = unavailable
    assert client.post(ROOT + '/sessions', json={'sessionId': 'p', 'declaredCount': 0}).status_code == 503
    assert not table.rows
    assert client.post(ROOT + '/sessions', json={'sessionId': 'p', 'declaredCount': 0}).status_code == 200


def test_api_exposes_bounded_evaluation_progress_until_last_page(api):
    client, table, library = api
    store = ShadowWaves(table, library[0])
    rows = [store.open_session('p' + str(i), 0) for i in range(21)]
    wave = rows[0]['waveId']
    for i, row in enumerate(rows):
        close(store, 'p' + str(i), row)
    first = client.post(ROOT + '/' + wave + '/evaluate', json={})
    assert first.json['scanInProgress'] is True
    assert first.json['scannedSessions'] == 20
    second = client.post(ROOT + '/' + wave + '/evaluate', json={})
    assert second.json['scanInProgress'] is False and second.json['state'] == 'ready'
    assert second.json['scannedSessions'] == 21


def test_reconcile_retries_source_completion_delivery_without_running_stage_work(source_world, monkeypatch):
    from fakes import FakeTable
    from processing_completion import prepare_completion
    store, table, wave, member = source_world
    metadata = FakeTable()
    row = dict(table.rows['library-A', 'original.jpg'], processingRevision=member['processingRevision'],
              processingGeneration='f' * 64)
    prepare_completion(row); metadata.upsert_entity(row)
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    reconcile_page(store, wave, metadata, table, lambda *a: [])
    assert metadata.get_entity('library-A', 'original.jpg')['processingCompletionPending'] == ''
    received = store._page('source-completion!')
    assert len(received) == 1 and received[0]['completionQualified'] is False
    assert not any(row.get('dispatchable') for row in table.rows.values())


def test_video_admission_and_observation_share_the_empty_backend_contract():
    from test_processing_completion import video_source
    table = AtomicTable(); store = ShadowWaves(table, 'lib')
    row = video_source()
    table.submit_transaction([('create', row)])
    session = store.open_session('video', 1)
    store.register('video', [{'itemId': 'i', 'filename': 'clip.mp4'}])
    resolved = resolve_source(store, table, 'video', 'i', 'accepted', 'clip.mp4')
    close(store, 'video', resolved)
    member = store._page(f"e!{session['waveId']}!m!")[0]
    assert json.loads(member['requiredSteps']) == []
    observation = observe_source(store, member, table, None,
        lambda *a: pytest.fail('video should not require a face lookup'))
    assert observation['outcome'] == 'settled'


def test_two_thousand_manifest_items_use_one_request_with_bounded_transactions(api):
    client, table, _ = api
    assert client.post(ROOT + '/sessions', json=dict(sessionId='large', declaredCount=2000)).status_code == 200
    items = [dict(itemId=f'i{i}', filename=f'holiday-photo-{i}.jpg') for i in range(2000)]
    assert len(json.dumps(dict(items=items)).encode()) > 65536
    response = client.post(ROOT + '/sessions/large/items', json=dict(items=items))
    assert response.status_code == 200 and response.json['registeredCount'] == 2000
    transactions = len(table.transactions)
    # A lost response can be replayed without changing counts or writing again.
    replay = client.post(ROOT + '/sessions/large/items', json=dict(items=items))
    assert replay.json['manifestRevision'] == response.json['manifestRevision']
    assert len(table.transactions) == transactions
    assert max(map(len, table.transactions)) <= 100


def test_manifest_failure_replays_partial_server_pages_safely(api, monkeypatch):
    client, table, library = api
    client.post(ROOT + '/sessions', json=dict(sessionId='partial', declaredCount=25))
    items = [dict(itemId=f'i{i}', filename=f'{i}.jpg') for i in range(25)]
    original = ShadowWaves.register
    failed = [False]
    def register(store, session, page):
        if page[0]['itemId'] == 'i20' and not failed[0]:
            failed[0] = True
            raise AzureError('storage unavailable')
        return original(store, session, page)
    monkeypatch.setattr(ShadowWaves, 'register', register)
    assert client.post(ROOT + '/sessions/partial/items', json=dict(items=items)).status_code == 503
    assert ShadowWaves(table, library[0])._required('session!partial')['registeredCount'] == 20
    response = client.post(ROOT + '/sessions/partial/items', json=dict(items=items))
    assert response.status_code == 200 and response.json['registeredCount'] == 25


def test_manifest_validates_last_item_before_any_page_is_written(api):
    client, table, library = api
    client.post(ROOT + '/sessions', json=dict(sessionId='invalid', declaredCount=21))
    items = [dict(itemId=f'i{i}', filename=f'{i}.jpg') for i in range(21)]
    items[-1]['filename'] = ''
    assert client.post(ROOT + '/sessions/invalid/items', json=dict(items=items)).status_code == 400
    assert ShadowWaves(table, library[0])._required('session!invalid')['registeredCount'] == 0
