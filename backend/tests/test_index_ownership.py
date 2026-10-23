"""Direct publisher boundaries and legacy handoff must fail before any I/O."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
import index_ownership
import storage_utils as su
from routes import admin, tools


PUBLISHERS = [
    ('storage_utils', name) for name in (
        'refresh_user_vector_index', 'refresh_user_people_embedding_index',
        'refresh_user_tag_embedding_index', 'refresh_user_search_db_incremental',
        'refresh_user_lexical_artifacts', 'refresh_user_lexical_index',
        'refresh_user_sort_index', 'refresh_user_access_index',
        'refresh_user_albums_index', 'refresh_user_people_index',
        'reconcile_search_db', 'prime_all_user_indexes_sequentially',
        'stream_library_artifacts', 'store_explore_summary', 'store_timeline_summary',
    )
] + [
    ('search_db', 'publish_delta'), ('search_db', 'create_empty_base'),
    ('search_db', 'compact_database'), ('search_db', 'write_for_snapshot'),
    ('library_build', 'bootstrap_library_build'),
    ('upload_hash_snapshot', 'rebuild_snapshot'),
    ('app', 'refresh_user_explore_summary'), ('app', 'refresh_user_timeline_summary'),
    ('app', '_streaming_lexical_build'), ('app', '_finalize_library_summaries'),
]


@pytest.mark.parametrize('role', ['core', 'upload', 'cluster', 'vision', 'archive', 'recovery', 'indexer'])
@pytest.mark.parametrize('module,name', PUBLISHERS)
def test_direct_catalog_publishers_reject_non_owner_before_io(monkeypatch, role, module, name):
    monkeypatch.setenv('APP_ROLE', role)
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    import vision_utils
    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: False)
    publisher = getattr(importlib.import_module(module), name)
    # Some publishers require more positional arguments. The ownership guard
    # must run even before entering those implementations/performing any I/O.
    with pytest.raises(index_ownership.IndexOwnershipError):
        publisher('library-A')


def test_cluster_cannot_run_the_catalog_job_body(monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    status = Mock(side_effect=AssertionError('job body entered'))
    monkeypatch.setattr(app, '_upsert_job_status', status)
    with pytest.raises(index_ownership.IndexOwnershipError):
        app._run_index_build_job('A', 'job', 'people')
    status.assert_not_called()


@pytest.mark.parametrize('module,name', [('storage_utils', 'ListingSink'), ('search_db', 'SearchDbSink')])
def test_direct_sink_finish_cannot_bypass_publisher_boundary(monkeypatch, module, name):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    sink = getattr(importlib.import_module(module), name)
    # An uninitialized instance also proves rejection precedes file/blob I/O.
    with pytest.raises(index_ownership.IndexOwnershipError):
        sink.finish(object.__new__(sink))


def test_vision_tag_exception_does_not_enable_other_catalogs(monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'vision')
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    import vision_utils
    monkeypatch.setattr(vision_utils, 'image_encoder_available', lambda: True)
    builder = Mock(return_value=None)
    monkeypatch.setattr(su, '_build_user_tag_embedding_index_snapshot', builder)
    assert su.refresh_user_tag_embedding_index('A') is None
    builder.assert_called_once()
    with pytest.raises(index_ownership.IndexOwnershipError):
        su.refresh_user_people_index('A')


@pytest.mark.parametrize('configured', ['cluster', 'cluster,vision', 'indexer', 'index-worker,cluster'])
def test_obsolete_build_role_configuration_is_rejected(monkeypatch, configured):
    monkeypatch.setenv('INDEX_BUILD_ROLES', configured)
    with pytest.raises(ValueError, match='only contain index-worker'):
        index_ownership.configured_build_roles()


def test_missing_index_queue_never_falls_back_to_library_ops(monkeypatch):
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', '')
    sender = Mock(side_effect=AssertionError('legacy library-ops queue used'))
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(send_message=sender))
    assert app.enqueue_index_build('A', scope='vector') == 'unavailable'
    sender.assert_not_called()


@pytest.mark.parametrize('kind,scope', [('index_build', 'people'), ('vector_index_rebuild', 'vector')])
def test_legacy_cluster_work_only_forwards_and_does_not_claim_completion(monkeypatch, kind, scope):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    enqueue = Mock(return_value='queued')
    statuses = Mock()
    monkeypatch.setattr(app, 'enqueue_index_build', enqueue)
    monkeypatch.setattr(app, '_upsert_job_status', statuses)
    monkeypatch.setattr(app, '_run_index_build_job', Mock(side_effect=AssertionError('cluster built index')))
    monkeypatch.setattr(app, 'refresh_user_vector_index', Mock(side_effect=AssertionError('vector bypass')))
    app._handle_clustering_queue_payload({'scope': 'people'}, 'legacy-job', 'A', kind)
    enqueue.assert_called_once_with('A', reason='legacy-index-handoff', scope=scope)
    assert statuses.call_args.kwargs['forwardedTo'] == app._index_build_job_id('A') + '-' + scope


def test_failed_legacy_handoff_raises_for_queue_redelivery(monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **k: 'unavailable')
    with pytest.raises(RuntimeError, match='handoff unavailable'):
        app._handle_clustering_queue_payload({}, 'old', 'A', 'vector_index_rebuild')


def test_cluster_cold_matching_keeps_candidates_without_publishing(monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    monkeypatch.setattr(app, 'person_table_client', object())
    monkeypatch.setattr(app, 'get_user_people_embedding_index', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_people_embedding_index_cache',
                        SimpleNamespace(get=lambda key, build: build()))
    monkeypatch.setattr(app, '_cached_person_rows_for_user', lambda uid: [
        {'RowKey': 'person-1', 'repEmbedding': '[1, 0]', 'faceIds': '["face-1"]'}])
    monkeypatch.setattr(app, '_active_face_ids_for_person', lambda *a: ['face-1'])
    monkeypatch.setattr(app, '_confirmed_face_count', lambda *a: 0)
    publish = Mock(side_effect=AssertionError('cluster published a catalog'))
    request = Mock()
    monkeypatch.setattr(app, 'refresh_user_people_embedding_index', publish)
    monkeypatch.setattr(su, 'request_index_build', request)
    candidates = app._load_people_embedding_index('A')
    assert [candidate['personId'] for candidate in candidates] == ['person-1']
    publish.assert_not_called()
    request.assert_called_once_with('A', kind='people_embedding')


@pytest.mark.parametrize('status,code', [('queued', 202), ('already_active', 202), ('unavailable', 503)])
def test_manual_vector_repair_uses_catalog_queue(monkeypatch, status, code):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('A', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    enqueue = Mock(return_value=status)
    monkeypatch.setattr(app, 'enqueue_index_build', enqueue)
    monkeypatch.setattr(app, '_enqueue_clustering_job', Mock(side_effect=AssertionError('cluster queue used')))
    with app.app.test_request_context('/api/admin/vector-index/rebuild', method='POST',
                                      json={'repair': True, 'confirm': 'REBUILD_VECTOR_INDEX'}):
        response, actual_code = admin.admin_rebuild_vector_index()
    assert actual_code == code
    enqueue.assert_called_once_with('A', reason='manual-vector-repair', scope='vector', coalesce=False)
    assert response.get_json()['success'] is (status != 'unavailable')


def test_forwarded_legacy_request_is_not_announced_as_finished(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('A', None))
    monkeypatch.setattr(app, '_maybe_sweep_old_job_rows', lambda user: None)
    now = app.datetime.now(app.timezone.utc).isoformat()
    rows = [dict(jobId='old', jobType='index_build', status='done', updatedAt=now,
                 forwardedTo='index-build-A-vector'),
            dict(jobId='index-build-A-vector', userId='A', jobType='index_build',
                 indexScope='vector', status='queued', updatedAt=now)]
    monkeypatch.setattr(app, 'jobs_table_client', SimpleNamespace(query_entities=lambda *a, **k: rows))
    with app.app.test_request_context('/api/jobs/status'):
        result = tools.jobs_status().get_json()['jobs']
    assert [job['jobId'] for job in result] == ['index-build-A-vector']
    assert result[0]['status'] == 'queued'
