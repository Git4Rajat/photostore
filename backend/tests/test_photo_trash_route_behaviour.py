"""Photo routes: bulk metadata, soft delete, trash restore, purge and rating glue."""

import pytest

import app


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_validate_media_filename', lambda name: name if isinstance(name, str) and name.endswith('.jpg') else None)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()



class TestSoftDelete:
    URL = '/api/photos/delete'

    def test_empty_or_all_invalid_request_deletes_nothing(self, http, monkeypatch):
        monkeypatch.setattr(app, '_soft_delete_photos_now', lambda *a: pytest.fail('nothing to delete'))
        assert http.post(self.URL, json={}).json == {'deleted': [], 'errors': ['Invalid request'], 'success': False}
        body = http.post(self.URL, json={'filenames': ['x.txt']}).json
        assert body['success'] is False and body['errors'] == ['x.txt: Invalid filename']

    def test_dedupes_names_and_merges_validation_and_delete_errors(self, http, monkeypatch):
        seen = []
        monkeypatch.setattr(app, '_soft_delete_photos_now',
                            lambda user, names: seen.append(names) or {'deleted': names, 'errors': ['b.jpg: gone'], 'success': True})
        body = http.post(self.URL, json={'filenames': ['a.jpg', 'a.jpg', 'bad.txt', 'b.jpg']}).json
        assert seen == [['a.jpg', 'b.jpg']]
        assert body['errors'] == ['bad.txt: Invalid filename', 'b.jpg: gone']

    def test_over_the_cap_is_413_and_does_nothing(self, http, monkeypatch):
        monkeypatch.setattr(app, 'BULK_MUTATION_MAX_ITEMS', 2)
        monkeypatch.setattr(app, '_soft_delete_photos_now', lambda *a: pytest.fail('over cap'))
        response = http.post(self.URL, json={'filenames': ['a.jpg', 'b.jpg', 'c.jpg']})
        assert response.status_code == 413 and response.json['success'] is False

    def test_async_queue_failure_is_503(self, http, monkeypatch):
        monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', 2)
        monkeypatch.setattr(app, '_enqueue_photo_soft_delete_job', lambda *a: {'status': 'unavailable'})
        response = http.post(self.URL, json={'filenames': ['a.jpg', 'b.jpg']})
        assert response.status_code == 503 and response.json['success'] is False


class TestRestore:
    URL = '/api/photos/trash/restore'

    @pytest.fixture
    def side_effects(self, monkeypatch):
        log = {'faces': [], 'search': [], 'people': 0, 'rebuild': [], 'cache': 0}
        monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda user: log.__setitem__('cache', log['cache'] + 1))
        monkeypatch.setattr(app, '_set_faces_source_deleted_for_filenames', lambda user, names, flag: log['faces'].append((names, flag)))
        monkeypatch.setattr(app, 'touch_user_search_indexes_state', lambda user, filenames=None: log['search'].append(filenames))
        monkeypatch.setattr(app, 'touch_user_people_index_state', lambda user: log.__setitem__('people', log['people'] + 1))
        monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda user, reason, scope: log['rebuild'].append((reason, scope)))
        return log

    def test_invalid_request_restores_nothing(self, http, side_effects):
        assert http.post(self.URL, json={'filenames': []}).json == {'restored': [], 'errors': ['Invalid request'], 'success': False}
        assert side_effects['rebuild'] == []

    def test_restores_found_photos_and_refreshes_indexes(self, http, monkeypatch, side_effects):
        monkeypatch.setattr(app, '_restore_deleted_file', lambda user, name: {} if name != 'gone.jpg' else None)
        body = http.post(self.URL, json={'filenames': ['a.jpg', 'gone.jpg', 'b.jpg']}).json
        assert body == {'restored': ['a.jpg', 'b.jpg'], 'errors': ['gone.jpg: Not found'], 'success': True}
        assert side_effects['faces'] == [(['a.jpg', 'b.jpg'], False)]
        assert side_effects['search'] == [['a.jpg', 'b.jpg']]
        assert side_effects['rebuild'] == [('photo-restore', 'people'), ('photo-restore', 'sort'), ('photo-restore', 'albums')] and side_effects['cache'] == 1

    def test_nothing_restored_means_no_index_work(self, http, monkeypatch, side_effects):
        monkeypatch.setattr(app, '_restore_deleted_file', lambda user, name: None)
        body = http.post(self.URL, json={'filenames': ['gone.jpg']}).json
        assert body['success'] is False
        assert side_effects['faces'] == [] and side_effects['rebuild'] == [] and side_effects['cache'] == 0

    def test_index_bookkeeping_failures_do_not_undo_the_restore(self, http, monkeypatch, side_effects):
        def boom(*a, **k):
            raise RuntimeError('index down')

        monkeypatch.setattr(app, '_restore_deleted_file', lambda user, name: {})
        for hook in ('_set_faces_source_deleted_for_filenames', 'touch_user_search_indexes_state',
                     'touch_user_people_index_state', '_trigger_indexer_rebuild'):
            monkeypatch.setattr(app, hook, boom)
        assert http.post(self.URL, json={'filenames': ['a.jpg']}).json['success'] is True


class TestPurge:
    URL = '/api/photos/trash/purge'

    def test_invalid_request_purges_nothing(self, http, monkeypatch):
        monkeypatch.setattr(app, '_hard_delete_photos_now', lambda *a: pytest.fail('nothing to purge'))
        assert http.post(self.URL, json={}).json == {'deleted': [], 'errors': ['Invalid request'], 'success': False}
        assert http.post(self.URL, json={'filenames': ['x.txt']}).json['errors'] == ['x.txt: Invalid filename']

    def test_purges_deduped_names_and_reports_partial_failure(self, http, monkeypatch):
        seen = []
        monkeypatch.setattr(app, '_hard_delete_photos_now',
                            lambda user, names: seen.append(names) or (['a.jpg'], ['b.jpg: blob locked']))
        body = http.post(self.URL, json={'filenames': ['a.jpg', 'a.jpg', 'b.jpg', 'bad.txt']}).json
        assert seen == [['a.jpg', 'b.jpg']]
        assert body == {'deleted': ['a.jpg'], 'errors': ['bad.txt: Invalid filename', 'b.jpg: blob locked'], 'success': True}

    def test_total_failure_is_not_success(self, http, monkeypatch):
        monkeypatch.setattr(app, '_hard_delete_photos_now', lambda user, names: ([], ['a.jpg: boom']))
        assert http.post(self.URL, json={'filenames': ['a.jpg']}).json['success'] is False

    def test_over_the_cap_is_413(self, http, monkeypatch):
        monkeypatch.setattr(app, 'BULK_MUTATION_MAX_ITEMS', 1)
        monkeypatch.setattr(app, '_hard_delete_photos_now', lambda *a: pytest.fail('over cap'))
        assert http.post(self.URL, json={'filenames': ['a.jpg', 'b.jpg']}).status_code == 413

    def test_large_batches_are_queued_with_202(self, http, monkeypatch):
        monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', 2)
        monkeypatch.setattr(app, '_hard_delete_photos_now', lambda *a: pytest.fail('must queue'))
        monkeypatch.setattr(app, '_enqueue_photo_hard_delete_job', lambda user, names: {'status': 'queued', 'jobId': 'j9'})
        response = http.post(self.URL, json={'filenames': ['a.jpg', 'b.jpg']})
        assert response.status_code == 202
        assert response.json['jobId'] == 'j9' and response.json['accepted'] == 2 and response.json['deleted'] == []

    def test_queue_failure_is_503(self, http, monkeypatch):
        monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', 1)
        monkeypatch.setattr(app, '_enqueue_photo_hard_delete_job', lambda *a: {'status': 'unavailable'})
        assert http.post(self.URL, json={'filenames': ['a.jpg']}).status_code == 503


class TestRating:
    def test_rejects_out_of_range_and_non_integer_ratings(self, http):
        for rating in (-1, 6, '3', 2.5, None):
            assert http.post('/api/photos/a.jpg/rating', json={'rating': rating}).status_code == 400

    def test_invalid_name_and_missing_photo(self, http, monkeypatch):
        assert http.post('/api/photos/a.txt/rating', json={'rating': 3}).status_code == 400
        monkeypatch.setattr(app, '_get_metadata_entity', lambda user, name: None)
        assert http.post('/api/photos/a.jpg/rating', json={'rating': 3}).status_code == 404

    def test_saves_rating_and_defaults_to_zero(self, http, monkeypatch):
        saved = []
        monkeypatch.setattr(app, '_get_metadata_entity', lambda user, name: {'RowKey': name})
        monkeypatch.setattr(app, '_update_metadata_entity_fields',
                            lambda user, name, fields: saved.append(fields) or dict(fields))
        assert http.post('/api/photos/a.jpg/rating', json={'rating': 5}).json == {'success': True, 'filename': 'a.jpg', 'rating': 5}
        assert http.post('/api/photos/a.jpg/rating', json={}).json['rating'] == 0
        assert saved == [{'rating': 5}, {'rating': 0}]

    def test_write_failure_is_503_and_unexpected_error_is_500(self, http, monkeypatch):
        monkeypatch.setattr(app, '_get_metadata_entity', lambda user, name: {'RowKey': name})
        monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda *a: None)
        response = http.post('/api/photos/a.jpg/rating', json={'rating': 1})
        assert response.status_code == 503 and response.json['code'] == 'metadata_write_failed'

        def boom(*a):
            raise RuntimeError('x')

        monkeypatch.setattr(app, '_update_metadata_entity_fields', boom)
        assert http.post('/api/photos/a.jpg/rating', json={'rating': 1}).status_code == 500
