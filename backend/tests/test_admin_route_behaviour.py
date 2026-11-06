"""Admin repair routes: confirmation gates, dry-run defaults, queue status mapping, validation."""
import contextlib

import pytest

import app


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_library_cut_over_to_waves', lambda user_id: False)
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


class TestWaveOwnedLibrary:
    """Wave-owned libraries process everything through upload waves. The legacy
    re-queue routes would reset a photo's steps and then queue nothing, so they
    must refuse before touching any row."""

    @pytest.fixture(autouse=True)
    def wave_owned(self, http, monkeypatch):
        # Depends on `http` so this override runs after its non-wave default.
        monkeypatch.setattr(app, '_library_cut_over_to_waves', lambda user_id: True)
        monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: pytest.fail('scanned rows'))
        monkeypatch.setattr(app, '_enqueue_processing_steps', lambda *a, **k: pytest.fail('reset steps'))
        monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **k: pytest.fail('queued work'))
        monkeypatch.setattr(app, '_get_metadata_entity', lambda *a, **k: pytest.fail('read a row'))

    def test_backfill_is_refused_even_with_a_valid_confirmation(self, http):
        response = http.post('/api/admin/backfill/photos', json={'repair': True, 'confirm': 'BACKFILL_ALL_PHOTOS'})
        assert response.status_code == 409 and response.json['code'] == 'wave_owned_library'

    def test_refusal_comes_before_validation_so_nothing_is_inspected(self, http):
        assert http.post('/api/admin/backfill/photos', json={}).status_code == 409

    def test_other_repair_routes_are_unaffected(self, http, repair_jobs):
        response = http.post('/api/admin/people/dedupe-faces', json={'repair': True, 'confirm': 'DEDUPE_FACES'})
        assert response.status_code == 200 and repair_jobs == [('u1', 'dedupe_faces', True)]
