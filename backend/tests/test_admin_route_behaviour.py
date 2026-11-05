"""Admin repair routes: confirmation gates, dry-run defaults, queue status mapping, validation."""
import contextlib

import pytest

import app


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()


@pytest.fixture
def repair_jobs(monkeypatch):
    calls = []

    def enqueue(user_id, *, action, dry_run):
        calls.append((user_id, action, dry_run))
        return {'status': 'queued', 'jobId': 'job-' + action}

    monkeypatch.setattr(app, '_enqueue_admin_repair_job', enqueue)
    return calls


# (url, confirm phrase, repair action queued)
REPAIR_ROUTES = [
    ('/api/admin/people/dedupe-faces', 'DEDUPE_FACES', 'dedupe_faces'),
    ('/api/admin/people/suppress-suspicious-faces', 'SUPPRESS_SUSPICIOUS_FACES', 'suppress_suspicious'),
    ('/api/admin/people/unblock-low-confidence-faces', 'UNBLOCK_LOW_CONFIDENCE_FACES', 'unblock_low_confidence'),
    ('/api/admin/people/rebuild-photo-people-index', 'REBUILD_PEOPLE_INDEX', 'rebuild_people_index'),
    ('/api/admin/people/repair-stale-memberships', 'REPAIR_STALE_MEMBERSHIPS', 'repair_stale_memberships'),
]


@pytest.mark.parametrize('url,confirm,action', REPAIR_ROUTES)
def test_repair_requires_repair_flag_and_exact_confirm(http, repair_jobs, url, confirm, action):
    for body in ({}, {'repair': True}, {'confirm': confirm}, {'repair': 'true', 'confirm': confirm},
                 {'repair': True, 'confirm': confirm.lower()}):
        response = http.post(url, json=body)
        assert response.status_code == 403
        assert response.json['code'] == 'protected_repair_required'
    assert repair_jobs == []


@pytest.mark.parametrize('url,confirm,action', REPAIR_ROUTES)
def test_repair_defaults_to_dry_run_and_only_applies_when_asked(http, repair_jobs, url, confirm, action):
    body = http.post(url, json={'repair': True, 'confirm': confirm}).json
    assert body == {'success': True, 'queued': True, 'jobId': 'job-' + action, 'status': 'queued'}
    http.post(url, json={'repair': True, 'confirm': confirm, 'dryRun': False})
    assert repair_jobs == [('u1', action, True), ('u1', action, False)]


@pytest.mark.parametrize('url,confirm,action', REPAIR_ROUTES)
def test_repair_unavailable_without_people_features(http, monkeypatch, repair_jobs, url, confirm, action):
    monkeypatch.setattr(app, '_people_features_available', lambda: False)
    assert http.post(url, json={'repair': True, 'confirm': confirm}).status_code == 503
    assert repair_jobs == []


@pytest.mark.parametrize('url,confirm,action', REPAIR_ROUTES)
def test_repair_reports_queue_failure_without_claiming_success(http, monkeypatch, url, confirm, action):
    monkeypatch.setattr(app, '_enqueue_admin_repair_job', lambda *a, **k: {'status': 'unavailable'})
    body = http.post(url, json={'repair': True, 'confirm': confirm}).json
    assert body['success'] is False and body['queued'] is False


def test_purge_orphaned_data_previews_by_default_and_gates_the_destructive_run(http, repair_jobs):
    url = '/api/admin/photos/purge-orphaned-data'
    assert http.post(url, json={}).status_code == 200
    assert repair_jobs[-1] == ('u1', 'purge_orphaned', True)
    refused = http.post(url, json={'dryRun': False, 'confirm': 'PURGE_ORPHANED_PHOTO_DATA'})
    assert refused.status_code == 403 and refused.json['code'] == 'protected_repair_required'
    assert http.post(url, json={'dryRun': False, 'repair': True, 'confirm': 'nope'}).status_code == 403
    assert len(repair_jobs) == 1
    assert http.post(url, json={'dryRun': False, 'repair': True, 'confirm': 'PURGE_ORPHANED_PHOTO_DATA'}).status_code == 200
    assert repair_jobs[-1] == ('u1', 'purge_orphaned', False)


class TestRecluster:
    URL = '/api/admin/people/recluster'
    BODY = {'repair': True, 'confirm': 'RECLUSTER_REPAIR'}

    def test_requires_confirmation(self, http, monkeypatch):
        monkeypatch.setattr(app, '_enqueue_clustering_job', lambda *a, **k: pytest.fail('must not enqueue'))
        assert http.post(self.URL, json={'repair': True}).status_code == 403
        assert http.post(self.URL, json={'confirm': 'RECLUSTER_REPAIR'}).status_code == 403

    def test_queues_a_non_forced_recluster_and_protects_confirmed_people_by_default(self, http, monkeypatch):
        seen = []
        monkeypatch.setattr(app, '_enqueue_clustering_job',
                            lambda user, **kw: seen.append((user, kw)) or {'status': 'queued', 'jobId': 'j1'})
        assert http.post(self.URL, json=self.BODY).json['jobId'] == 'j1'
        http.post(self.URL, json={**self.BODY, 'allowReassignConfirmed': True})
        assert seen == [
            ('u1', {'force': False, 'job_type': 'people_recluster', 'allow_reassign_confirmed': False}),
            ('u1', {'force': False, 'job_type': 'people_recluster', 'allow_reassign_confirmed': True}),
        ]

    @pytest.mark.parametrize('status,code', [('unavailable', 503), ('failed', 500), ('already_active', 200)])
    def test_maps_queue_status_to_http_status(self, http, monkeypatch, status, code):
        monkeypatch.setattr(app, '_enqueue_clustering_job', lambda *a, **k: {'status': status})
        assert http.post(self.URL, json=self.BODY).status_code == code

    def test_unexpected_error_is_a_json_500(self, http, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError('queue exploded')
        monkeypatch.setattr(app, '_enqueue_clustering_job', boom)
        response = http.post(self.URL, json=self.BODY)
        assert response.status_code == 500 and 'queue exploded' not in response.get_data(as_text=True)


class TestRestoreSnapshot:
    URL = '/api/admin/people/recluster/restore'

    def test_requires_snapshot_id(self, http):
        assert http.post(self.URL, json={}).status_code == 400
        assert http.post(self.URL, json={'snapshotId': '   '}).status_code == 400

    def test_restores_trimmed_snapshot_id(self, http, monkeypatch):
        seen = []
        monkeypatch.setattr(app, '_restore_people_repair_snapshot',
                            lambda user, snap: seen.append((user, snap)) or {'success': True, 'restored': 3})
        assert http.post(self.URL, json={'snapshotId': ' snap-1 '}).json == {'success': True, 'restored': 3}
        assert seen == [('u1', 'snap-1')]

    def test_unknown_snapshot_is_404_with_the_failure_body(self, http, monkeypatch):
        monkeypatch.setattr(app, '_restore_people_repair_snapshot', lambda *a: {'success': False, 'error': 'missing'})
        response = http.post(self.URL, json={'snapshotId': 'nope'})
        assert response.status_code == 404 and response.json['error'] == 'missing'


class TestRebuildVectorIndex:
    URL = '/api/admin/vector-index/rebuild'
    BODY = {'repair': True, 'confirm': 'REBUILD_VECTOR_INDEX'}

    def test_requires_confirmation(self, http, monkeypatch):
        monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **k: pytest.fail('must not enqueue'))
        assert http.post(self.URL, json={'confirm': 'REBUILD_VECTOR_INDEX'}).status_code == 403

    @pytest.mark.parametrize('status,code,accepted', [('queued', 202, True), ('already_active', 202, True), ('failed', 503, False)])
    def test_status_mapping(self, http, monkeypatch, status, code, accepted):
        seen = []
        monkeypatch.setattr(app, 'enqueue_index_build', lambda user, **kw: seen.append(kw) or status)
        monkeypatch.setattr(app, '_index_build_job_id', lambda user: 'idx-' + user)
        response = http.post(self.URL, json=self.BODY)
        assert response.status_code == code
        assert response.json['queued'] is accepted
        assert response.json['jobId'] == ('idx-u1-vector' if accepted else None)
        assert seen == [{'reason': 'manual-vector-repair', 'scope': 'vector', 'coalesce': False}]

    def test_enqueue_exception_is_a_json_500(self, http, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError('x')
        monkeypatch.setattr(app, 'enqueue_index_build', boom)
        assert http.post(self.URL, json=self.BODY).status_code == 500


class TestBackfillPhotos:
    URL = '/api/admin/backfill/photos'
    BODY = {'repair': True, 'confirm': 'BACKFILL_ALL_PHOTOS'}

    @pytest.fixture
    def backfill(self, monkeypatch):
        state = {'rows': [], 'enqueued': [], 'ipwork': [], 'filters': [], 'invalidated': []}

        def iterate(user, **kw):
            state['filters'].append(kw.get('extra_filter'))
            return iter(state['rows'])

        monkeypatch.setattr(app, 'PROCESSING_MODE', 'backend')
        monkeypatch.setattr(app, 'ipwork_queue_client', object())
        monkeypatch.setattr(app, '_iter_metadata_rows_for_user', iterate)
        monkeypatch.setattr(app, '_enqueue_processing_steps',
                            lambda user, name, steps, force: state['enqueued'].append((name, tuple(steps), force)) or {s: {'status': 'queued'} for s in steps})
        monkeypatch.setattr(app, '_queue_ipwork_processing',
                            lambda user, name, steps: state['ipwork'].append(name) or {'status': 'queued'})
        monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda user: state['invalidated'].append(user))
        return state

    def test_requires_confirmation(self, http, backfill):
        assert http.post(self.URL, json={'repair': True}).status_code == 403
        assert backfill['enqueued'] == []

    @pytest.mark.parametrize('steps', [[], 'ocr', ['bogus'], ['ocr', 'bogus']])
    def test_rejects_invalid_steps(self, http, backfill, steps):
        response = http.post(self.URL, json={**self.BODY, 'steps': steps})
        assert response.status_code == 400 and response.json['code'] == 'invalid_steps'

    @pytest.mark.parametrize('cursor', ['', 5, 'x' * 1025])
    def test_rejects_invalid_continuation(self, http, backfill, cursor):
        response = http.post(self.URL, json={**self.BODY, 'continuation': cursor})
        assert response.status_code == 400 and response.json['code'] == 'invalid_continuation'

    def test_backend_mode_without_queue_is_unavailable(self, http, backfill, monkeypatch):
        monkeypatch.setattr(app, 'ipwork_queue_client', None)
        assert http.post(self.URL, json=self.BODY).status_code == 503

    def test_queues_photos_skips_trashed_and_videos_and_dedupes_steps(self, http, backfill):
        backfill['rows'] = [
            {'RowKey': 'a.jpg', 'processing_state': 'done'},
            {'RowKey': 'trashed.jpg', 'processing_state': ' Deleted '},
            {'RowKey': 'clip.mp4'},
            {'RowKey': 'b.jpg'},
        ]
        body = http.post(self.URL, json={**self.BODY, 'steps': ['ocr', 'ocr', 'face']}).json
        assert (body['queued'], body['skipped'], body['failed'], body['complete']) == (2, 2, 0, True)
        assert body['steps'] == ['ocr', 'face'] and body['continuation'] is None
        assert backfill['enqueued'] == [('a.jpg', ('ocr', 'face'), True), ('b.jpg', ('ocr', 'face'), True)]
        assert backfill['invalidated'] == ['u1']

    def test_stops_after_ten_rows_and_returns_a_resumable_cursor(self, http, backfill):
        backfill['rows'] = [{'RowKey': f'p{i:02d}.jpg'} for i in range(11)]
        body = http.post(self.URL, json=self.BODY).json
        assert body['total'] == 10 and body['complete'] is False and body['continuation'] == 'p09.jpg'
        http.post(self.URL, json={**self.BODY, 'continuation': "o'brien.jpg"})
        assert backfill['filters'][-1] == "RowKey gt 'o''brien.jpg'"

    def test_one_failing_photo_is_counted_not_fatal(self, http, backfill, monkeypatch):
        backfill['rows'] = [{'RowKey': 'bad.jpg'}, {'RowKey': 'good.jpg'}]

        def enqueue(user, name, steps, force):
            if name == 'bad.jpg':
                raise RuntimeError('table down')
            return {s: {'status': 'queued'} for s in steps}

        monkeypatch.setattr(app, '_enqueue_processing_steps', enqueue)
        body = http.post(self.URL, json=self.BODY).json
        assert (body['queued'], body['failed']) == (1, 1)

    def test_a_step_that_did_not_reset_to_queued_is_a_failure(self, http, backfill, monkeypatch):
        backfill['rows'] = [{'RowKey': 'a.jpg'}]
        monkeypatch.setattr(app, '_enqueue_processing_steps', lambda *a, **k: {'ocr': {'status': 'skipped'}})
        assert http.post(self.URL, json=self.BODY).json['failed'] == 1

    def test_metadata_scan_failure_returns_503_with_partial_counts(self, http, backfill, monkeypatch):
        def broken(user, **kw):
            yield {'RowKey': 'a.jpg'}
            raise RuntimeError('page failed')

        monkeypatch.setattr(app, '_iter_metadata_rows_for_user', broken)
        response = http.post(self.URL, json=self.BODY)
        assert response.status_code == 503
        assert response.json['queued'] == 1 and response.json['complete'] is False

    def test_browser_mode_expects_skipped_queue_result(self, http, backfill, monkeypatch):
        monkeypatch.setattr(app, 'PROCESSING_MODE', 'browser')
        monkeypatch.setattr(app, 'ipwork_queue_client', None)
        monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **k: {'status': 'skipped'})
        backfill['rows'] = [{'RowKey': 'a.jpg'}]
        assert http.post(self.URL, json=self.BODY).json['queued'] == 1


class TestEnqueueIpwork:
    URL = '/api/admin/ipwork/enqueue'

    @pytest.fixture
    def ipwork(self, monkeypatch):
        state = {'enqueued': [], 'queued': []}
        entities = {'a.jpg': {'processing_state': 'done'}, 'gone.jpg': {'processing_state': 'deleted'},
                    'clip.mp4': {'processing_state': 'done'}}
        monkeypatch.setattr(app, '_get_metadata_entity', lambda user, name: entities.get(name))
        monkeypatch.setattr(app, '_enqueue_processing_steps',
                            lambda user, name, steps, force=False: state['enqueued'].append((name, force)))
        monkeypatch.setattr(app, '_queue_ipwork_processing', lambda user, name, steps: state['queued'].append(name))
        return state

    @pytest.mark.parametrize('body,code', [
        ({'filenames': ['a.jpg']}, 'invalid_steps'),
        ({'steps': [], 'filenames': ['a.jpg']}, 'invalid_steps'),
        ({'steps': ['nope'], 'filenames': ['a.jpg']}, 'invalid_steps'),
        ({'steps': ['ocr']}, 'invalid_filenames'),
        ({'steps': ['ocr'], 'filenames': []}, 'invalid_filenames'),
        ({'steps': ['ocr'], 'filenames': ['a.jpg'] * 2001}, 'too_many_filenames'),
    ])
    def test_validation(self, http, ipwork, body, code):
        response = http.post(self.URL, json=body)
        assert response.status_code == 400 and response.json['code'] == code

    def test_queues_existing_photos_and_skips_missing_trashed_and_video(self, http, ipwork):
        body = http.post(self.URL, json={'steps': ['ocr'], 'filenames': ['a.jpg', 'missing.jpg', 'gone.jpg', 'clip.mp4'], 'force': 1}).json
        assert (body['queued'], body['skipped'], body['total']) == (1, 3, 4)
        assert ipwork['enqueued'] == [('a.jpg', True)] and ipwork['queued'] == ['a.jpg']

    def test_force_defaults_off_and_a_failing_photo_is_skipped(self, http, ipwork, monkeypatch):
        http.post(self.URL, json={'steps': ['ocr'], 'filenames': ['a.jpg']})
        assert ipwork['enqueued'] == [('a.jpg', False)]
        monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
        assert http.post(self.URL, json={'steps': ['ocr'], 'filenames': ['a.jpg']}).json['skipped'] == 1


class TestVerifyFaceIndex:
    URL = '/api/admin/people/verify-face-index'

    def test_validation(self, http):
        assert http.post(self.URL, json={}).json['code'] == 'invalid_filenames'
        assert http.post(self.URL, json={'filenames': ['a.jpg'] * 501}).json['code'] == 'too_many_filenames'

    def test_all_invalid_names_rejected_and_valid_ones_forwarded(self, http, monkeypatch):
        monkeypatch.setattr(app, '_validate_media_filename', lambda name: name if name.endswith('.jpg') else None)
        assert http.post(self.URL, json={'filenames': ['../x', '']}).status_code == 400
        seen = []
        monkeypatch.setattr(app, '_verify_face_filename_indexes', lambda user, names: seen.append(names) or {'checked': len(names)})
        assert http.post(self.URL, json={'filenames': ['a.jpg', 'bad.txt', 'b.jpg']}).json == {'checked': 2}
        assert seen == [['a.jpg', 'b.jpg']]

    def test_unavailable_without_people_features(self, http, monkeypatch):
        monkeypatch.setattr(app, '_people_features_available', lambda: False)
        assert http.post(self.URL, json={'filenames': ['a.jpg']}).status_code == 503


class TestJobStatus:
    URL = '/api/admin/jobs/status'

    @pytest.fixture
    def rows(self, monkeypatch):
        table = {}
        monkeypatch.setattr(app, '_get_job_row', lambda user, job: table.get(job))
        return table

    def test_missing_or_unknown_job_is_unknown(self, http, rows):
        assert http.get(self.URL).json == {'status': 'unknown'}
        assert http.get(self.URL + '?jobId=nope').json == {'status': 'unknown'}

    def test_returns_status_and_parses_json_result(self, http, rows):
        rows['j1'] = {'userId': 'u1', 'status': 'completed', 'result': '{"fixed": 2}'}
        assert http.get(self.URL + '?jobId=j1').json == {'status': 'completed', 'result': {'fixed': 2}, 'error': None}
        rows['j2'] = {'userId': 'u1', 'status': 'failed', 'result': 'not json', 'error': 'boom'}
        assert http.get(self.URL + '?jobId=j2').json == {'status': 'failed', 'result': 'not json', 'error': 'boom'}

    def test_other_users_job_is_hidden(self, http, rows):
        rows['j1'] = {'userId': 'someone-else', 'status': 'completed', 'result': '{}'}
        assert http.get(self.URL + '?jobId=j1').json == {'status': 'unknown'}

    def test_follows_exactly_one_same_user_alias(self, http, rows):
        rows['old'] = {'userId': 'u1', 'status': 'queued', 'forwardedTo': 'new'}
        assert http.get(self.URL + '?jobId=old').json['status'] == 'queued'
        rows['new'] = {'userId': 'u1', 'status': 'completed', 'result': {'ok': 1}}
        assert http.get(self.URL + '?jobId=old').json['result'] == {'ok': 1}
        rows['new'] = {'userId': 'u1', 'status': 'completed', 'forwardedTo': 'again'}
        assert http.get(self.URL + '?jobId=old').json == {'status': 'unknown'}
        rows['new'] = {'userId': 'other', 'status': 'completed'}
        assert http.get(self.URL + '?jobId=old').json == {'status': 'unknown'}


class TestRecoverSourceReservations:
    URL = '/api/admin/indexes/source-reservations'

    @pytest.fixture
    def recovery(self, monkeypatch):
        import automatic_index_updates as automatic
        import index_artifact_cleanup

        calls = []
        monkeypatch.setattr(app, '_require_admin', lambda **k: ('lib1', None))
        monkeypatch.setattr(app, '_require_library_context', lambda **k: ('op1', 'lib1', None))
        monkeypatch.setattr(index_artifact_cleanup, 'artifact_catalog_serialization',
                            lambda library, storage=None: contextlib.nullcontext('guard'))
        monkeypatch.setattr(automatic, 'operator_fence', lambda library, **kw: calls.append(('fence', library, kw)) or {'fenceId': 'f1'})
        monkeypatch.setattr(automatic, 'operator_prove', lambda library, fence, **kw: calls.append(('prove', fence, kw)) or {'proved': True})
        monkeypatch.setattr(automatic, 'recover_quiesced_source_reservations', lambda library, **kw: calls.append(('recover', kw)) or {'recovered': 1})
        monkeypatch.setattr(automatic, 'resume_quiesced_sources', lambda library, **kw: calls.append(('resume', kw)) or {'resumed': 1})
        return calls

    def test_non_admin_is_rejected_before_any_recovery_work(self, http, monkeypatch, recovery):
        monkeypatch.setattr(app, '_require_admin', lambda **k: (None, (app.jsonify({'error': 'admin only'}), 403)))
        assert http.post(self.URL, json={'action': 'fence'}).status_code == 403
        assert recovery == []

    def test_fence_records_operator_and_reason(self, http, recovery):
        assert http.post(self.URL, json={'action': 'fence', 'reason': 'maintenance'}).json == {'fenceId': 'f1'}
        assert recovery == [('fence', 'lib1', {'operator': 'op1', 'reason': 'maintenance'})]

    @pytest.mark.parametrize('action', ['prove', 'recover', 'resume', 'bogus', None])
    def test_other_actions_need_a_fence_id(self, http, recovery, action):
        response = http.post(self.URL, json={'action': action})
        assert response.status_code == 400 and 'fenceId' in response.json['error']
        assert recovery == []

    def test_prove_requires_the_writers_stopped_confirmation(self, http, recovery):
        response = http.post(self.URL, json={'action': 'prove', 'fenceId': 'f1'})
        assert response.status_code == 400 and 'ALL_LIBRARY_WRITERS_STOPPED' in response.json['error']
        assert recovery == []

    def test_prove_forwards_the_operator_attestations(self, http, recovery):
        body = {'action': 'prove', 'fenceId': 'f1', 'confirm': 'ALL_LIBRARY_WRITERS_STOPPED', 'reason': 'r',
                'allWritersStopped': True, 'noPausedRequests': True, 'legacyWritersStopped': False}
        assert http.post(self.URL, json=body).json == {'proved': True}
        _, fence, kw = recovery[0]
        assert fence == 'f1' and kw['guard'] == 'guard' and kw['operator'] == 'op1'
        assert (kw['all_writers_stopped'], kw['no_paused_requests'], kw['legacy_writers_stopped']) == (True, True, False)

    def test_recover_and_resume_use_the_fence_under_the_catalog_guard(self, http, recovery):
        assert http.post(self.URL, json={'action': 'recover', 'fenceId': 'f1'}).json == {'recovered': 1}
        assert http.post(self.URL, json={'action': 'resume', 'fenceId': 'f1'}).json == {'resumed': 1}
        assert recovery == [('recover', {'guard': 'guard', 'fence_id': 'f1'}), ('resume', {'guard': 'guard', 'fence_id': 'f1'})]

    def test_unknown_action_with_fence_is_400(self, http, recovery):
        response = http.post(self.URL, json={'action': 'bogus', 'fenceId': 'f1'})
        assert response.status_code == 400 and response.json['error'] == 'Unknown recovery action'


def test_repair_routes_are_not_admin_gated_today(http, monkeypatch, repair_jobs):
    """Documents current authorization: only the source-reservation route calls
    _require_admin. Every other /api/admin/* route accepts any signed-in user who
    sends the confirm phrase. If these become admin-only, update this test."""
    monkeypatch.setattr(app, '_require_admin', lambda **k: pytest.fail('repair routes do not consult _require_admin'))
    response = http.post('/api/admin/people/dedupe-faces', json={'repair': True, 'confirm': 'DEDUPE_FACES', 'dryRun': False})
    assert response.status_code == 200
    assert repair_jobs == [('u1', 'dedupe_faces', False)]
