import json

import pytest
from azure.core.exceptions import ResourceModifiedError

import storage_utils
from processing_completion import StaleProcessingResult
from processing_fence import ResultFence, current_fence, result_fence, write_artifact
from test_face_by_filename_lookup import FilenameLookupTable, _face
from test_metadata_write_concurrency import _RacyTable


@pytest.fixture
def world(monkeypatch):
    metadata = _RacyTable()
    row = dict(PartitionKey='lib', RowKey='photo.jpg', anonymousImageId='photo', processingRevision='rev',
        processingGeneration='b' * 64, processingFence='attempt', processing_lease_owner='owner',
        processing_lease_expires_at='2099-01-01T00:00:00+00:00')
    metadata.upsert_entity(row)
    faces, embeddings, lookup = FilenameLookupTable(), FilenameLookupTable(), FilenameLookupTable()
    for name, table in [('metadata', metadata), ('face', faces), ('face_embeddings', embeddings), ('face_by_filename', lookup)]:
        monkeypatch.setitem(storage_utils._CTX, name + '_table_client', table)
    for key in ('person_table_client', 'person_members_table_client', 'face_summary_lookup', 'face_summary_cache_writer'):
        monkeypatch.setitem(storage_utils._CTX, key, None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', None)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_start_face_crop_warming', lambda *a, **k: None)
    return metadata, faces, embeddings, lookup, ResultFence(metadata, 'lib', 'photo.jpg', 'rev', 'attempt', 'owner', 'photo',
        generation='b' * 64)


def test_attempt_context_is_scoped_and_cleared_after_failure(world):
    *_, fence = world
    with pytest.raises(RuntimeError):
        with result_fence(fence):
            assert current_fence('lib', 'photo.jpg') is fence
            with pytest.raises(StaleProcessingResult): current_fence('other-library')
            raise RuntimeError('compute failed')
    assert current_fence() is None


@pytest.mark.parametrize('change', [{'processingRevision': 'replacement'}, {'processingFence': 'new-attempt'},
    {'processing_lease_owner': 'other'}, {'anonymousImageId': 'other-photo'},
    {'processing_lease_expires_at': '2000-01-01T00:00:00+00:00'}, {'processingGeneration': 'c' * 64}])
def test_stale_attempt_cannot_write_output(world, change):
    metadata, faces, _, _, fence = world
    row = metadata.get_entity('lib', 'photo.jpg'); row.update(change); metadata.upsert_entity(row)
    with result_fence(fence), pytest.raises(StaleProcessingResult):
        write_artifact(faces, dict(PartitionKey='lib', RowKey='face', filename='photo.jpg'))
    assert not faces.rows


@pytest.mark.parametrize('operation', ['claim', 'commit'])
def test_policy_cache_cannot_hide_cleanup_from_source_claim_or_commit(world, monkeypatch, operation):
    from processing_cleanup import source_policy_scope, check_source_policy
    from test_wave_activation import active
    from test_wave_recovery import RuntimeTable
    metadata, *_ = world
    jobs = RuntimeTable()
    store = active(jobs, 'lib')
    monkeypatch.setitem(storage_utils._CTX, 'source_policy_check', lambda uid: check_source_policy(jobs, uid))
    with source_policy_scope(jobs, 'lib'):
        row = jobs.get_entity(store.partition, 'policy')
        jobs.update_entity(dict(row, cleanupState='cleaning'), etag=row.metadata['etag'], match_condition='IfNotModified')
        baseline = metadata.get_entity('lib', 'photo.jpg')
        with pytest.raises(RuntimeError, match='cleanup'):
            if operation == 'claim':
                storage_utils.claim_processing_lease('lib', 'photo.jpg', 'other-owner')
            else:
                storage_utils._commit_worker_processing_results('lib', 'photo.jpg', baseline,
                    dict(baseline), {}, 'owner')
    assert metadata.update_calls == 0


def test_cached_policy_does_not_cache_photo_generation_checks(world, monkeypatch):
    import processing_fence
    from processing_cleanup import source_policy_scope, check_source_policy
    from test_wave_activation import active
    from test_wave_recovery import RuntimeTable
    metadata, faces, _, _, fence = world
    jobs = RuntimeTable()
    active(jobs, 'lib')
    monkeypatch.setattr(processing_fence, 'POLICY_CHECK', lambda uid: check_source_policy(jobs, uid))
    with source_policy_scope(jobs, 'lib'), result_fence(fence):
        fence.check()
        row = metadata.get_entity('lib', 'photo.jpg')
        metadata.upsert_entity(dict(row, processingFence='replacement'))
        with pytest.raises(StaleProcessingResult):
            write_artifact(faces, dict(PartitionKey='lib', RowKey='face', filename='photo.jpg'))
    assert not faces.rows


def test_artifact_cas_rejects_newer_curation_instead_of_retrying_with_its_version(world):
    _, faces, _, _, fence = world
    faces.upsert_entity(dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', personId='old'))
    baseline = faces.get_entity('lib', 'face')
    faces.upsert_entity(dict(baseline, personId='user-curated'))
    with result_fence(fence), pytest.raises(ResourceModifiedError):
        write_artifact(faces, dict(baseline, confidence=.9), previous=baseline, previous_known=True)
    assert faces.get_entity('lib', 'face')['personId'] == 'user-curated'


def test_late_write_cannot_replace_an_artifact_changed_after_source_check(world, monkeypatch):
    metadata, faces, _, _, fence = world
    faces.upsert_entity(dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', confidence=.8))
    original = metadata.get_entity
    def change_between_check_and_write(*a, **k):
        row = original(*a, **k)
        faces.upsert_entity(dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', confidence=.99))
        return row
    monkeypatch.setattr(metadata, 'get_entity', change_between_check_and_write)
    with result_fence(fence), pytest.raises(ResourceModifiedError):
        write_artifact(faces, dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', confidence=.2))
    assert faces.get_entity('lib', 'face')['confidence'] == .99


def test_real_face_and_embedding_and_lookup_writes_carry_the_same_attempt(world):
    _, faces, embeddings, lookup, fence = world
    with result_fence(fence):
        ids = storage_utils._store_client_face_entities('lib', 'photo.jpg', [_face(0)])
    assert len(ids) == 1
    for row in [faces.get_entity('lib', ids[0]), embeddings.get_entity('lib', ids[0]), lookup.get_entity('lib', 'photo.jpg')]:
        assert row['processingFence'] == 'attempt'
        assert row['processingRevision'] == 'rev'
        assert row['sourcePhotoId'] == 'photo'
    assert lookup.get_entity('lib', 'photo.jpg')['state'] == 'complete'


def test_source_replaced_during_embedding_write_cannot_publish_face_or_complete_lookup(world, monkeypatch):
    metadata, faces, embeddings, lookup, fence = world
    create = embeddings.create_entity
    def replacement(entity):
        create(entity)
        row = metadata.get_entity('lib', 'photo.jpg'); row['processingFence'] = 'successor'
        metadata.upsert_entity(row)
    monkeypatch.setattr(embeddings, 'create_entity', replacement)
    with result_fence(fence), pytest.raises(StaleProcessingResult):
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [_face(0)])
    assert not faces.rows
    assert lookup.get_entity('lib', 'photo.jpg')['state'] != 'complete'


def test_fenced_destructive_reconcile_fails_before_any_mutation(world):
    _, faces, embeddings, lookup, fence = world
    with result_fence(fence), pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='not yet supported'):
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [], force_reconcile=True)
    assert not faces.rows and not embeddings.rows and not lookup.rows


def test_opt_in_claimed_browser_commit_requires_attempt_token_before_effects(world, monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('missing token reached effects'))
    with pytest.raises(StaleProcessingResult, match='token changed or missing'):
        storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg',
            expected_processing_revision='rev', lease_owner='owner')


def test_late_create_never_overwrites_a_face_created_after_the_missing_read(world, monkeypatch):
    from azure.core.exceptions import ResourceExistsError
    metadata, faces, _, _, fence = world
    original = metadata.get_entity
    def successor_creates(*a, **k):
        row = original(*a, **k)
        faces.create_entity(dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', confidence=.99))
        return row
    monkeypatch.setattr(metadata, 'get_entity', successor_creates)
    with result_fence(fence), pytest.raises(ResourceExistsError):
        write_artifact(faces, dict(PartitionKey='lib', RowKey='face', filename='photo.jpg', confidence=.2))
    assert faces.get_entity('lib', 'face')['confidence'] == .99


def test_zero_face_result_publishes_an_explicit_attempt_bound_empty_lookup(world):
    _, faces, embeddings, lookup, fence = world
    with result_fence(fence):
        assert storage_utils._store_client_face_entities('lib', 'photo.jpg', []) == []
    row = lookup.get_entity('lib', 'photo.jpg')
    assert row['state'] == 'complete' and json.loads(row['faceIds']) == []
    assert row['processingFence'] == 'attempt' and row['sourcePhotoId'] == 'photo'
    assert not faces.rows and not embeddings.rows


def test_face_writes_keep_one_fresh_source_check_per_artifact(world, monkeypatch):
    *_, fence = world
    original = ResultFence.check
    checks = []
    def check(self):
        checks.append(self.attempt)
        return original(self)
    monkeypatch.setattr(ResultFence, 'check', check)
    with result_fence(fence):
        ids = storage_utils._store_client_face_entities('lib', 'photo.jpg', [_face(0), _face(40)])
    assert len(ids) == 2
    # Filename acquire + publish, and a check at each of two writes per face.
    # Filename lease checks no longer duplicate those artifact checks.
    assert len(checks) == 6


def test_source_change_during_embedding_read_still_blocks_persistence(world, monkeypatch):
    metadata, faces, embeddings, _, fence = world
    read = embeddings.get_entity
    def changed(*args, **kwargs):
        row = metadata.get_entity('lib', 'photo.jpg')
        metadata.upsert_entity(dict(row, processingFence='replacement-attempt'))
        return read(*args, **kwargs)
    monkeypatch.setattr(embeddings, 'get_entity', changed)
    with result_fence(fence), pytest.raises(StaleProcessingResult):
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [_face(0)])
    assert not faces.rows and not embeddings.rows
