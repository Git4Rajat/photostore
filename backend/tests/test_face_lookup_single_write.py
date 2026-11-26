"""A photo with no face to store publishes its filename lookup in one create, with the row the two-step path leaves."""
import json

import pytest
from azure.core.exceptions import ResourceExistsError

import storage_utils
from tests.test_face_by_filename_lookup import lookup_ctx, _face  # noqa: F401


def two_step(monkeypatch, user, filename, faces, **kwargs):
    monkeypatch.setattr(storage_utils, '_publish_faceless_filename_lookup', lambda *a, **k: (False, None))
    return storage_utils._store_client_face_entities(user, filename, faces, **kwargs)


def comparable(row):
    return {key: value for key, value in dict(row).items() if key != 'generation'}


@pytest.fixture(autouse=True)
def count_lookup_writes(lookup_ctx):
    lookup = lookup_ctx[1]
    lookup.write_calls = 0
    for name in ('create_entity', 'update_entity'):
        original = getattr(lookup, name)
        def counted(*args, _original=original, **kwargs):
            lookup.write_calls += 1
            return _original(*args, **kwargs)
        setattr(lookup, name, counted)


def lookup_writes(lookup):
    """Create and replace calls the lookup table has served so far."""
    return lookup.write_calls


@pytest.mark.parametrize('faces', [[], [{'confidence': 0.0, 'bbox': {'left': 0, 'top': 0, 'width': 0, 'height': 0}}], ['junk']])
def test_new_faceless_photo_is_published_with_one_write_and_the_same_row(lookup_ctx, monkeypatch, faces):
    face_table, lookup = lookup_ctx
    assert storage_utils._store_client_face_entities('u1', 'new.jpg', faces) == []
    assert lookup_writes(lookup) == 1  # The two-step path writes the row twice.
    assert storage_utils.get_face_ids_for_filename('u1', 'new.jpg') == []
    assert face_table.writes == []
    single = comparable(lookup.rows[('u1', 'new.jpg')])

    face_table2, lookup2 = lookup_ctx
    lookup2.rows.clear(); lookup2.versions.clear(); lookup2.write_calls = 0
    two_step(monkeypatch, 'u1', 'new.jpg', faces)
    assert lookup_writes(lookup2) == 2
    assert comparable(lookup2.rows[('u1', 'new.jpg')]) == single


def test_faces_already_stored_for_the_filename_are_published_not_dropped(lookup_ctx):
    face_table, lookup = lookup_ctx
    stored = storage_utils._store_client_face_entities('u1', 'legacy.jpg', [_face(0)])
    lookup.rows.clear(); lookup.versions.clear(); lookup.write_calls = 0  # A photo whose faces predate the lookup row.
    assert storage_utils._store_client_face_entities('u1', 'legacy.jpg', []) == []
    assert storage_utils.get_face_ids_for_filename('u1', 'legacy.jpg') == stored
    assert lookup_writes(lookup) == 1
    assert len(face_table.query_entities("PartitionKey eq 'u1'", None)) >= 1  # Nothing was deleted.


def test_an_existing_lookup_row_takes_the_two_step_path(lookup_ctx):
    face_table, lookup = lookup_ctx
    storage_utils._store_client_face_entities('u1', 'again.jpg', [])
    before = lookup_writes(lookup)
    storage_utils._store_client_face_entities('u1', 'again.jpg', [])
    assert lookup_writes(lookup) == before + 2


def test_a_photo_with_faces_takes_the_two_step_path(lookup_ctx):
    face_table, lookup = lookup_ctx
    ids = storage_utils._store_client_face_entities('u1', 'faces.jpg', [_face(0)])
    assert len(ids) == 1
    assert lookup_writes(lookup) >= 2


def test_force_reconcile_takes_the_two_step_path(lookup_ctx):
    face_table, lookup = lookup_ctx
    storage_utils._store_client_face_entities('u1', 'forced.jpg', [], force_reconcile=True)
    assert lookup_writes(lookup) == 2


def test_a_writer_that_began_first_is_not_overwritten(lookup_ctx, monkeypatch):
    """The exclusive create loses to a marker written after the read; the two-step path then refuses as before."""
    face_table, lookup = lookup_ctx
    original = lookup.create_entity
    def racing_create(entity):
        if entity['state'] == 'complete' and ('u1', 'raced.jpg') not in lookup.rows:
            lookup.rows[('u1', 'raced.jpg')] = dict(entity, state='writing', generation='f' * 32,
                leaseExpiresAt='2999-01-01T00:00:00+00:00', faceIds='[]')
            lookup.versions[('u1', 'raced.jpg')] = 1
        return original(entity)
    monkeypatch.setattr(lookup, 'create_entity', racing_create)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError):
        storage_utils._store_client_face_entities('u1', 'raced.jpg', [])
    row = lookup.rows[('u1', 'raced.jpg')]
    assert row['state'] == 'writing' and row['generation'] == 'f' * 32


def test_source_fence_is_checked_before_and_stamped_on_the_single_write(lookup_ctx):
    from processing_fence import ResultFence, result_fence
    face_table, lookup = lookup_ctx

    class Fence:
        checks = 0
        def check(self):
            Fence.checks += 1
        def headers(self):
            return {'sourceFence': 'f1'}
    fence = Fence()
    import processing_fence
    original = processing_fence.current_fence
    processing_fence.current_fence = lambda user, filename=None: fence
    try:
        storage_utils._store_client_face_entities('u1', 'fenced.jpg', [])
    finally:
        processing_fence.current_fence = original
    assert Fence.checks == 1  # The two-step path checks twice.
    assert lookup.rows[('u1', 'fenced.jpg')]['sourceFence'] == 'f1'


def test_a_failed_fence_check_writes_nothing(lookup_ctx):
    face_table, lookup = lookup_ctx
    import processing_fence

    class Fence:
        def check(self):
            raise RuntimeError('source changed')
        def headers(self):
            return {}
    original = processing_fence.current_fence
    processing_fence.current_fence = lambda user, filename=None: Fence()
    try:
        with pytest.raises(RuntimeError, match='source changed'):
            storage_utils._store_client_face_entities('u1', 'stale.jpg', [])
    finally:
        processing_fence.current_fence = original
    assert ('u1', 'stale.jpg') not in lookup.rows


def test_the_fallback_does_not_read_the_lookup_row_twice(lookup_ctx, monkeypatch):
    face_table, lookup = lookup_ctx
    storage_utils._store_client_face_entities('u1', 'again.jpg', [])
    lookup.gets.clear()
    storage_utils._store_client_face_entities('u1', 'again.jpg', [])
    reads = sum(1 for key in lookup.gets if key == ('u1', 'again.jpg'))
    lookup.gets.clear()
    two_step(monkeypatch, 'u1', 'again.jpg', [])
    assert reads == sum(1 for key in lookup.gets if key == ('u1', 'again.jpg'))
